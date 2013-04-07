from datetime import datetime

from fabric.api import local, run, sudo, prefix, cd, env
from fabric.contrib import django
from fabric.contrib.files import upload_template, append, comment, uncomment
from fabric.contrib.console import confirm
from fabric.operations import get
from fabric import tasks
from fabric.utils import abort, puts

from django.conf import settings as django_settings

import settings

env.path = local('pwd')
# Allow fabric to restart apache2
env.always_use_pty = False

class BaseTask(tasks.Task):

    def run(self):
        if not hasattr(env, 'name'):
            abort('You must specify an environment to deploy to\n\n'\
                  'e.g. fab production prepare_environment')

    def get_local_settings(self):
        django.settings_module('settings.development')
        return django_settings

    def select_env(self, name='staging'):
        env.name = name

        django.settings_module('settings.%(name)s' % env)
        for attr in ['DOMAIN', 'USER', 'HOST', 'PASSWORD', 'REPO', 'BRANCH']:
            if not hasattr(django_settings, 'FABRIC_%s' % attr):
                abort('You must set FABRIC_%s in your settings file' % attr)

        env.domain = django_settings.FABRIC_DOMAIN
        env.user = django_settings.FABRIC_USER
        env.hosts = [django_settings.FABRIC_HOST]
        env.password = django_settings.FABRIC_PASSWORD
        env.repo = django_settings.FABRIC_REPO
        env.branch = django_settings.FABRIC_BRANCH
        
        env.virtual_env = '$WORKON_HOME/%(domain)s' % env
        env.release_path = '/data/web/%(domain)s' % env

        # Number of previous deploy releases to keep
        env.release_count = getattr(django_settings, 'FABRIC_RELEASES', 5)



class Production(BaseTask):
    """
    Run tasks in production environment
    """
    name = 'production'

    def run(self):
        self.select_env('production')


class Staging(BaseTask):
    """
    Run tasks in staging environment
    """
    name = 'staging'

    def run(self):
        self.select_env('staging')


class Bootstrap(BaseTask):
    """
    Prepare a virtualenv with apache2 and nginx config files
    """
    name = 'bootstrap'

    def run(self):
        super(Bootstrap, self).run()
        self.create_virtualenv()
        self.clone_git_repo()
        self.create_folders()
        self.upload_config_files()

    def create_virtualenv(self):
        #sudo('chmod 777 $WORKON_HOME/hook.log')
        run('source .bashrc')
        run('mkvirtualenv %(domain)s' % env)
        # append env vars to virtualenv activate file
        append('%(virtual_env)s/bin/activate' % env, ['',
            'export DJANGO_SETTINGS_MODULE=settings.%(name)s' % env,
            'export PYTHONPATH=%(release_path)s/releases/current' % env,
            ''])

    def clone_git_repo(self):
        # Checkout git repo from cahoona VM-3
        run('mkdir -p %(release_path)s/releases' % env)
        with cd('%(release_path)s/releases' % env):
            env.now = datetime.now().strftime('%Y%m%d%H%M%S')
            run('git clone %(repo)s %(now)s' % env)
            with cd(env.now):
                run('git checkout %(branch)s' % env)

        # symlink project to current release
        run('ln -s %(release_path)s/releases/%(now)s %(release_path)s/releases/current' % env)

    def create_folders(self):
        with cd(env.release_path):
            run('mkdir -p {media,static,apache,log}')
            run('chmod 777 {media,log}')

    def upload_config_files(self):
        vhost_root = '/var/www/vhosts/%(domain)s' % env
        kwargs = dict(context=env, use_sudo=True, backup=False)
        # wsgi
        upload_template('%(real_fabfile)s/conf/wsgi.conf' % env,
                        '%(release_path)s/apache/%(name)s.wsgi' % env,
                        **kwargs)
        # apache
        upload_template('%(real_fabfile)s/conf/apache.conf' % env,\
                    '/etc/apache2/sites-available/%(domain)s' % env, **kwargs)
        sudo('a2ensite %(domain)s' % env)
        sudo('service apache2 restart')

        # nginx
        upload_template('%(real_fabfile)s/conf/nginx.conf' % env,\
                    '/etc/nginx/sites-available/%(domain)s' % env, **kwargs)
        sudo('ln -sf /etc/nginx/sites-available/%(domain)s '\
             '/etc/nginx/sites-enabled/%(domain)s' % env)
        sudo('service nginx restart')


class BaseDeploy(BaseTask):

    def update_and_migrate(self):
        with cd('%(release_path)s/releases/current' % env):
            # remove placeholders
            run('find . -name placeholder -delete')

            # migrate + collect static files
            with prefix('workon %(domain)s' % env):
                run('pip install -r requirements.txt')
                run('python ./manage.py migrate --all')
                run('python ./manage.py collectstatic --noinput')

    def remove_old_releases(self):
        with cd('%(release_path)s/releases' % env):
            # Tidy up (remove) old releases
            while int(run('ls -1 | wc -l')) >  (env.release_count + 1):
                run('rm -Rf $(ls . | sort -f | head -n 1)')


class Deploy(BaseDeploy):
    """
    Deploy project to remote server
    """
    name = 'deploy'

    def run(self):
        super(Deploy, self).run()
        #sudo('chmod 777 $WORKON_HOME/hook.log')

        with cd('%(release_path)s/releases' % env):
            env.now = datetime.now().strftime('%Y%m%d%H%M%S')
            run('git clone %(repo)s %(now)s' % env)
            with cd(env.now):
                run('git checkout %(branch)s' % env)

            # symlink project to current release
            run('rm -f current')
            run('ln -s %(release_path)s/releases/%(now)s %(release_path)s/releases/current' % env)

        self.remove_old_releases()
        self.update_and_migrate()
        sudo('service apache2 graceful')


class Rollback(BaseDeploy):
    """
    Rollback remote project to previous release if one exists
    """
    name = 'rollback'

    def run(self, update_requirements=True, migrate=True, static=True):
        super(Rollback, self).run()
        with cd('%(virtual_env)s/releases' % env):
            # Only rollback if we have previous releases
            if int(run('ls -1 | wc -l')) < 2:
                abort('There is no previous release to rollback to')

            # Get previous release
            output = run("ls . | sort -f | grep -v '^current$' $1")
            previous_release = output.split()[-1]

            # Move previous release to current
            run('ln -sf ' + previous_release + ' %(release_path)s/releases/current' % env)

        self.remove_old_releases()
        self.update_and_migrate()
        sudo('service apache2 graceful')


class Test(tasks.Task):
    """
    Run tests locally
    """
    name = 'test'

    def run(self):
        with prefix('source ../bin/activate'):
            with cd('$VIRTUAL_ENV/project'):
                local('django-admin.py validate')
                local('django-admin.py test')


class CreateDatabase(BaseTask):
    """
    Create and populate remote database (with syncdb)
    """
    name = 'create_database'

    def run(self, run_migrations=False):
        remote_db_settings = {
            'USER': 'root',
            'PASSWORD': env.password,
            'NAME': env.domain.replace('.', '_')}

        sudo('mysql -u%(USER)s -p%(PASSWORD)s -e '\
             '"CREATE DATABASE %(NAME)s"' % remote_db_settings)
        with prefix('workon %(domain)s' % env):
            migs = ' --migrate' if run_migrations else ''
            run('django-admin.py syncdb --noinput %s' % migs)


class SyncLocalDatabase(BaseTask):
    """
    Download database from remote server to local env
    """
    name = 'sync_local_database'

    def run(self):
        super(SyncLocalDatabase, self).run()
        dump = '/tmp/%(domain)s.json' % env
        # Remote
        with cd('%(virtual_env)s/project' % env):
            with prefix('workon %(domain)s' % env):
                run('django-admin.py dumpdata > %s --exclude=contenttypes' %\
                    dump)

        # Local
        with prefix('source ../bin/activate'):
            with cd('$VIRTUAL_ENV/project'):
                get(dump, '/tmp/')
                local('django-admin.py syncdb --noinput --migrate')
                local('django-admin.py flush --noinput')
                local('django-admin.py loaddata %s' % dump)


class SyncLocalMedia(BaseTask):
    """
    Download media from remote server to local env
    """
    name = 'sync_local_media'

    def run(self):
        super(SyncLocalMedia, self).run()
        remote_dir = '/var/www/vhosts/%(domain)s/media' % env
        tar = '/tmp/%(domain)s.tar' % env

        # Remote
        with cd(remote_dir):
            run('tar -czf %s .' % tar)
        
        # Local
        get(tar, '/tmp')
        with prefix('source ../bin/activate'):
            local_settings = self.get_local_settings()
            local('mkdir -p %s' % local_settings.MEDIA_ROOT)
            local('chmod 777 %s' % local_settings.MEDIA_ROOT)
            with cd(local_settings.MEDIA_ROOT):
                local('tar -C %s -xvf %s' %\
                      (local_settings.MEDIA_ROOT, tar))

class VirtualenvPermission(tasks.Task):

    name = 'virtualenv_permission'

    def run(self):
        sudo('chmod 777 $WORKON_HOME/hook.log')


class RemoteDjangoAdmin(BaseTask):
    "Run django-admin.py commands remotely"
    name = 'django_admin'

    def run(self, cmd=None):
        super(RemoteDjangoAdmin, self).run()
        if cmd:
            with prefix('workon %(domain)s' % env):
                run('django-admin.py %s' % cmd)
        else:
            abort('You must specify a command (e.g. sycdb --migrate)')


virtualenv_permission = VirtualenvPermission()
production = Production()
staging  = Staging()
bootstrap = Bootstrap()
deploy = Deploy()
rollback = Rollback()
test  = Test()
create_database = CreateDatabase()
sync_local_database = SyncLocalDatabase()
sync_local_media = SyncLocalMedia()
django_admin = RemoteDjangoAdmin()
