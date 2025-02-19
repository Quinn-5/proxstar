import os
import json
import time
import atexit
import logging
import subprocess
import psutil
import psycopg2

# from gunicorn_conf import start_websockify
import rq_dashboard
from rq import Queue
from redis import Redis
from rq_scheduler import Scheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    abort,
    url_for,
    jsonify,
    Response,
)
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.rq import RqIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from proxstar.db import (
    Base,
    datetime,
    get_pool_cache,
    renew_vm_expire,
    set_user_usage_limits,
    get_template,
    get_templates,
    get_allowed_users,
    add_ignored_pool,
    delete_ignored_pool,
    add_allowed_user,
    delete_allowed_user,
    get_template_disk,
    set_template_info,
    add_shared_pool,
    get_shared_pool,
    get_shared_pools,
)
from proxstar.vnc import (
    add_vnc_target,
    get_vnc_targets,
    delete_vnc_target,
    stop_websockify,
    open_vnc_session,
)
from proxstar.auth import get_auth
from proxstar.util import gen_password
from proxstar.starrs import check_hostname, renew_ip
from proxstar.proxmox import connect_proxmox, get_isos, get_pools, get_ignored_pools

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)

app = Flask(__name__)
app.config.from_object(rq_dashboard.default_settings)
if os.path.exists(os.path.join(app.config.get('ROOT_DIR', os.getcwd()), 'config_local.py')):
    config = os.path.join(app.config.get('ROOT_DIR', os.getcwd()), 'config_local.py')
else:
    config = os.path.join(app.config.get('ROOT_DIR', os.getcwd()), 'config.py')
app.config.from_pyfile(config)
app.config['GIT_REVISION'] = (
    subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('utf-8').rstrip()
)

# Sentry setup
sentry_sdk.init(
    dsn=app.config['SENTRY_DSN'],
    integrations=[FlaskIntegration(), RqIntegration(), SqlalchemyIntegration()],
    environment=app.config['SENTRY_ENV'],
)

auth = get_auth(app)

redis_conn = Redis(app.config['REDIS_HOST'], app.config['REDIS_PORT'])
q = Queue(connection=redis_conn, default_timeout=360)
scheduler = Scheduler(connection=redis_conn)

engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
Base.metadata.bind = engine
DBSession = sessionmaker(bind=engine)
db = DBSession()

if app.config['USE_STARRS']:
    starrs = psycopg2.connect(
        "dbname='{}' user='{}' host='{}' password='{}'".format(
            app.config['STARRS_DB_NAME'],
            app.config['STARRS_DB_USER'],
            app.config['STARRS_DB_HOST'],
            app.config['STARRS_DB_PASS'],
        )
    )

from proxstar.vm import VM
from proxstar.user import User
from proxstar.tasks import (
    generate_pool_cache_task,
    process_expiring_vms_task,
    cleanup_vnc_task,
    delete_vm_task,
    create_vm_task,
    setup_template_task,
)

if 'generate_pool_cache' not in scheduler:
    logging.info('adding generate pool cache task to scheduler')
    scheduler.schedule(
        id='generate_pool_cache',
        scheduled_time=datetime.datetime.utcnow(),
        func=generate_pool_cache_task,
        interval=90,
    )

if 'process_expiring_vms' not in scheduler:
    logging.info('adding process expiring VMs task to scheduler')
    scheduler.cron('0 5 * * *', id='process_expiring_vms', func=process_expiring_vms_task)

if 'cleanup_vnc' not in scheduler:
    logging.info('adding cleanup VNC task to scheduler')
    scheduler.schedule(
        id='cleanup_vnc',
        scheduled_time=datetime.datetime.utcnow(),
        func=cleanup_vnc_task,
        interval=3600,
    )


def add_rq_dashboard_auth(blueprint):
    @blueprint.before_request
    @auth.oidc_auth
    def rq_dashboard_auth(*args, **kwargs):  # pylint: disable=unused-argument,unused-variable
        if 'rtp' not in session['userinfo']['groups']:
            abort(403)


rq_dashboard_blueprint = rq_dashboard.blueprint
add_rq_dashboard_auth(rq_dashboard_blueprint)
app.register_blueprint(rq_dashboard_blueprint, url_prefix='/rq')


@app.errorhandler(404)
def not_found(e):
    try:
        user = User(session['userinfo']['preferred_username'])
        return render_template('404.html', user=user, e=e), 404
    except KeyError as exception:
        print(exception)
        return render_template('404.html', user='chom', e=e), 404


@app.errorhandler(403)
def forbidden(e):
    try:
        user = User(session['userinfo']['preferred_username'])
        return render_template('403.html', user=user, e=e), 403
    except KeyError as exception:
        print(exception)
        return render_template('403.html', user='chom', e=e), 403


@app.route('/')
@app.route('/user/<string:user_view>')
@auth.oidc_auth
def list_vms(user_view=None):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if app.config['FORCE_STANDARD_USER']:
        user.rtp = False
    if user_view:
        if not user.rtp:
            abort(403)
        user_view = User(user_view)
        vms = user_view.vms
        for pending_vm in user_view.pending_vms:
            vm = next((vm for vm in vms if vm['name'] == pending_vm['name']), None)
            if vm:
                vms[vms.index(vm)]['status'] = pending_vm['status']
                vms[vms.index(vm)]['pending'] = True
            else:
                vms.append(pending_vm)
    else:
        if user.active:
            vms = user.vms
            for pending_vm in user.pending_vms:
                vm = next((vm for vm in vms if vm['name'] == pending_vm['name']), None)
                if vm:
                    vms[vms.index(vm)]['status'] = pending_vm['status']
                    vms[vms.index(vm)]['pending'] = True
                else:
                    vms.append(pending_vm)
        else:
            vms = 'INACTIVE'
    return render_template('list_vms.html', user=user, external_view=user_view, vms=vms)


@app.route('/pool/shared/<string:name>')
@auth.oidc_auth
def list_shared_vms(name=None):
    user = User(session['userinfo']['preferred_username'])
    pool = get_shared_pool(db, name)
    if pool:
        if user.name in pool.members or user.rtp:
            proxmox = connect_proxmox()
            vms = proxmox.pools(pool.name).get()['members']
        else:
            return 'Not Member of Pool', 403
    else:
        return 'Pool does not exist', 400
    if app.config['FORCE_STANDARD_USER']:
        user.rtp = False
    return render_template('list_vms.html', user=user, external_view=pool, vms=vms)


@app.route('/pools')
def list_pools():
    user = User(session['userinfo']['preferred_username'])
    if app.config['FORCE_STANDARD_USER']:
        user.rtp = False
    proxmox = connect_proxmox()
    user_pools = get_pool_cache(db) if user.rtp else []
    shared_pools = map(
        lambda pool: {
            'name': pool.name,
            'members': pool.members,
            'vms': proxmox.pools(pool.name).get()['members'],
        },
        get_shared_pools(db, user.name, user.rtp),
    )
    return render_template(
        'list_pools.html', user=user, user_pools=user_pools, shared_pools=shared_pools
    )


@app.route('/isos')
@auth.oidc_auth
def isos():
    proxmox = connect_proxmox()
    stored_isos = get_isos(proxmox, app.config['PROXMOX_ISO_STORAGE'])
    return json.dumps({'isos': stored_isos})


@app.route('/hostname/<string:name>')
@auth.oidc_auth
def hostname(name):
    valid, available = check_hostname(starrs, name) if app.config['USE_STARRS'] else (True, True)
    if not valid:
        return 'invalid'
    if not available:
        return 'taken'
    else:
        return 'ok'


@app.route('/vm/<string:vmid>')
@auth.oidc_auth
def vm_details(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        usage_check = user.check_usage(vm.cpu, vm.mem, 0)
        return render_template(
            'vm_details.html',
            user=user,
            vm=vm,
            usage=user.usage,
            limits=user.limits,
            usage_check=usage_check,
        )
    else:
        return abort(403)


@app.route('/vm/<string:vmid>/power/<string:action>', methods=['POST'])
@auth.oidc_auth
def vm_power(vmid, action):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        vnc_token_key = f'vnc_token|{vmid}'
        # For deleting the token from redis later
        vnc_token = None
        try:
            vnc_token = redis_conn.get(vnc_token_key).decode('utf-8')
        except AttributeError as e:
            print(
                f'Warning: Could not get vnc_token during {action}:{e}. {action} is still being performed.'
            )
        if action == 'start':
            vmconfig = vm.config
            usage_check = user.check_usage(vmconfig['cores'], vmconfig['memory'], 0)
            if usage_check:
                return usage_check
            vm.start()
        elif action == 'stop':
            vm.stop()
            if vnc_token is not None:
                delete_vnc_target(token=vnc_token)
                redis_conn.delete(vnc_token_key)
        elif action == 'shutdown':
            vm.shutdown()
            if vnc_token is not None:
                delete_vnc_target(token=vnc_token)
                redis_conn.delete(vnc_token_key)
        elif action == 'reset':
            vm.reset()
        elif action == 'suspend':
            vm.suspend()
            if vnc_token is not None:
                delete_vnc_target(token=vnc_token)
                redis_conn.delete(vnc_token_key)
        elif action == 'resume':
            vm.resume()
        return '', 200
    else:
        return '', 403


@app.route('/console/vm/<string:vmid>', methods=['POST'])
@auth.oidc_auth
def vm_console(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        # import pdb; pdb.set_trace()
        vm = VM(vmid)
        vnc_ticket, vnc_port = open_vnc_session(
            vmid, vm.node, app.config['PROXMOX_USER'], app.config['PROXMOX_PASS']
        )
        node = f'{vm.node}.csh.rit.edu'
        token = add_vnc_target(node, vnc_port)
        redis_conn.set(f'vnc_token|{vmid}', str(token))  # Store the VNC token in Redis.
        return {
            'host': app.config['VNC_HOST'],
            'port': app.config['VNC_PORT'],
            'token': token,
            'password': vnc_ticket,
        }, 200

    else:
        return '', 403


@app.route('/vm/<string:vmid>/cpu/<int:cores>', methods=['POST'])
@auth.oidc_auth
def vm_cpu(vmid, cores):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        cur_cores = vm.cpu
        if cores >= cur_cores:
            if vm.qmpstatus in ('running', 'paused'):
                usage_check = user.check_usage(cores - cur_cores, 0, 0)
            else:
                usage_check = user.check_usage(cores, 0, 0)
            if usage_check:
                return usage_check
        vm.set_cpu(cores)
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/mem/<int:mem>', methods=['POST'])
@auth.oidc_auth
def vm_mem(vmid, mem):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        cur_mem = vm.mem // 1024
        if mem >= cur_mem:
            if vm.qmpstatus in ('running', 'paused'):
                usage_check = user.check_usage(0, mem - cur_mem, 0)
            else:
                usage_check = user.check_usage(0, mem, 0)
            if usage_check:
                return usage_check
        vm.set_mem(mem * 1024)
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/disk/<string:disk>/<int:size>', methods=['POST'])
@auth.oidc_auth
def vm_disk(vmid, disk, size):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        usage_check = user.check_usage(0, 0, size)
        if usage_check:
            return usage_check
        vm.resize_disk(disk, size)
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/renew', methods=['POST'])
@auth.oidc_auth
def vm_renew(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        renew_vm_expire(db, vmid, app.config['VM_EXPIRE_MONTHS'])
        for interface in vm.interfaces:
            if interface[2] != 'No IP' and app.config['USE_STARRS']:
                renew_ip(starrs, interface[2])
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/eject', methods=['POST'])
@auth.oidc_auth
def iso_eject(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        vm = VM(vmid)
        vm.eject_iso()
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/mount/<string:iso>', methods=['POST'])
@auth.oidc_auth
def iso_mount(vmid, iso):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        iso = '{}:iso/{}'.format(app.config['PROXMOX_ISO_STORAGE'], iso)
        vm = VM(vmid)
        vm.mount_iso(iso)
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/delete', methods=['POST'])
@auth.oidc_auth
def delete(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        # send_stop_ssh_tunnel(vmid)
        # Submit the delete VM task to RQ
        q.enqueue(delete_vm_task, vmid)
        return '', 200
    else:
        return '', 403


@app.route('/vm/<string:vmid>/boot_order', methods=['POST'])
@auth.oidc_auth
def set_boot_order(vmid):
    user = User(session['userinfo']['preferred_username'])
    connect_proxmox()
    if user.rtp or int(vmid) in user.allowed_vms:
        boot_order = []
        for key in sorted(request.form):
            boot_order.append(request.form[key])
        vm = VM(vmid)
        vm.set_boot_order(boot_order)
        return '', 200
    else:
        return '', 403


@app.route('/vm/create', methods=['GET', 'POST'])
@auth.oidc_auth
def create():
    user = User(session['userinfo']['preferred_username'])
    proxmox = connect_proxmox()
    if user.active or user.rtp:
        if request.method == 'GET':
            stored_isos = get_isos(proxmox, app.config['PROXMOX_ISO_STORAGE'])
            pools = get_pools(proxmox, db)
            for pool in get_shared_pools(db, user.name, True):
                pools.append(pool.name)
            templates = get_templates(db)
            return render_template(
                'create_vm.html',
                user=user,
                usage=user.usage,
                limits=user.limits,
                percents=user.usage_percent,
                isos=stored_isos,
                pools=pools,
                templates=templates,
            )
        elif request.method == 'POST':
            name = request.form['name'].lower()
            cores = request.form['cores']
            memory = request.form['mem']
            template = request.form['template']
            disk = request.form['disk']
            iso = request.form['iso']
            ssh_key = request.form['ssh_key']
            if iso != 'none':
                iso = '{}:iso/{}'.format(app.config['PROXMOX_ISO_STORAGE'], iso)
            if not user.rtp:
                if template == 'none':
                    usage_check = user.check_usage(0, 0, disk)
                else:
                    usage_check = user.check_usage(cores, memory, disk)
                username = user.name
            else:
                usage_check = None
                username = request.form['user']
            if usage_check:
                return usage_check
            else:
                valid, available = (
                    check_hostname(starrs, name) if app.config['USE_STARRS'] else (True, True)
                )

                if valid and available:
                    if template == 'none':
                        q.enqueue(
                            create_vm_task,
                            username,
                            name,
                            cores,
                            memory,
                            disk,
                            iso,
                            job_timeout=300,
                        )
                    else:
                        q.enqueue(
                            setup_template_task,
                            template,
                            name,
                            username,
                            ssh_key,
                            cores,
                            memory,
                            job_timeout=600,
                        )
                        return '', 200
            return '', 200
        return None
    else:
        return '', 403


@app.route('/limits/<string:user>', methods=['POST'])
@auth.oidc_auth
def set_limits(user):
    if 'rtp' in session['userinfo']['groups']:
        cpu = request.form['cpu']
        mem = request.form['mem']
        disk = request.form['disk']
        set_user_usage_limits(db, user, cpu, mem, disk)
        return '', 200
    else:
        return '', 403


@app.route('/user/<string:user>/delete', methods=['POST'])
@auth.oidc_auth
def delete_user(user):
    if 'rtp' in session['userinfo']['groups']:
        connect_proxmox()
        User(user).delete()
        return '', 200
    else:
        return '', 403


@app.route('/settings')
@auth.oidc_auth
def settings():
    user = User(session['userinfo']['preferred_username'])
    if user.rtp:
        templates = get_templates(db)
        db_ignored_pools = get_ignored_pools(db)
        db_allowed_users = get_allowed_users(db)
        return render_template(
            'settings.html',
            user=user,
            templates=templates,
            ignored_pools=db_ignored_pools,
            allowed_users=db_allowed_users,
        )
    else:
        return abort(403)


@app.route('/pool/<string:pool>/ignore', methods=['POST', 'DELETE'])
@auth.oidc_auth
def ignored_pools(pool):
    if 'rtp' in session['userinfo']['groups']:
        if request.method == 'POST':
            add_ignored_pool(db, pool)
        elif request.method == 'DELETE':
            delete_ignored_pool(db, pool)
        return '', 200
    else:
        return '', 403


@app.route('/pool/shared/create', methods=['GET', 'POST'])
@auth.oidc_auth
def create_shared_pool():
    user = User(session['userinfo']['preferred_username'])
    if request.method == 'GET':
        return render_template('create_pool.html', user=user)
    elif request.method == 'POST':
        name = request.form['name']
        members = request.form['members'].split(',')
        description = request.form['description']
        if 'rtp' in session['userinfo']['groups']:
            try:
                proxmox = connect_proxmox()
                proxmox.pools.post(poolid=name, comment=description)
            except:
                return 'Error creating pool', 400
            add_shared_pool(db, name, members)
            return '', 200
        else:
            return '', 403


@app.route('/pool/shared/<string:name>/modify', methods=['POST'])
@auth.oidc_auth
def modify_shared_pool(name):
    members = request.form['members'].split(',')
    if 'rtp' in session['userinfo']['groups']:
        pool = get_shared_pool(db, name)
        if pool:
            pool.members = members
            db.commit()
            return '', 200
        return 'Pool not found', 400
    else:
        return '', 403


@app.route('/pool/shared/<string:name>/delete', methods=['POST'])
@auth.oidc_auth
def delete_shared_pool(name):
    if 'rtp' in session['userinfo']['groups']:
        pool = get_shared_pool(db, name)
        if pool:
            db.delete(pool)
            db.commit()
            proxmox = connect_proxmox()
            proxmox.pools(name).delete()
            return '', 200
        return 'Pool not found', 400
    else:
        return '', 403


@app.route('/user/<string:user>/allow', methods=['POST', 'DELETE'])
@auth.oidc_auth
def allowed_users(user):
    if 'rtp' in session['userinfo']['groups']:
        if request.method == 'POST':
            add_allowed_user(db, user)
        elif request.method == 'DELETE':
            delete_allowed_user(db, user)
        return '', 200
    else:
        return '', 403


@app.route('/console/cleanup', methods=['POST'])
def cleanup_vnc():
    if request.form['token'] == app.config['VNC_CLEANUP_TOKEN']:
        print('Cleaning up targets file...')
        with open(app.config['WEBSOCKIFY_TARGET_FILE'], 'w') as targets:
            targets.truncate()
        print('Clearing vnc tokens from Redis...')
        count = 0
        ns_keys = 'vnc_token*'
        for key in redis_conn.scan_iter(ns_keys):
            redis_conn.delete(key)
            count += 1
        print(f'Deleted {count} key(s).')
        return '', 200
    print('Got bad cleanup request')
    return '', 403


@app.route('/template/<string:template_id>/disk')
@auth.oidc_auth
def template_disk(template_id):
    if template_id == 'none':
        return '0'
    return get_template_disk(db, template_id)


@app.route('/template/<string:template_id>/edit', methods=['POST'])
@auth.oidc_auth
def template_edit(template_id):
    if 'rtp' in session['userinfo']['groups']:
        name = request.form['name']
        disk = request.form['disk']
        set_template_info(db, template_id, name, disk)
        return '', 200
    else:
        return '', 403


@app.route('/logout')
@auth.oidc_logout
def logout():
    return redirect(url_for('list_vms'), 302)


@app.route('/health')
def health():
    """
    Shows an ok status if the application is up and running
    """
    return jsonify({'status': 'ok'})


def exit_handler():
    stop_websockify()


atexit.register(exit_handler)

if __name__ == '__main__':
    app.run(threaded=False)
