# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import simple_audit
import logging
import datetime
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _
from django.db.models.signals import pre_save, post_save, pre_delete
from django.dispatch import receiver
from django_extensions.db.fields.encrypted import EncryptedCharField
from django.utils.functional import cached_property
from util import slugify, make_db_random_password
from util.models import BaseModel
from physical.models import DatabaseInfra, Environment, Plan
from drivers import factory_for
from system.models import Configuration
from datetime import date, timedelta

from account.models import Team

from drivers.base import ConnectionError, DatabaseStatus


LOG = logging.getLogger(__name__)
MB_FACTOR = 1.0 / 1024.0 / 1024.0
GB_FACTOR = 1.0 / 1024.0 / 1024.0 / 1024.0


class Project(BaseModel):
    name = models.CharField(
        verbose_name=_("Project name"), max_length=100, unique=True)
    description = models.TextField(
        verbose_name=_("Description"), null=True, blank=True)
    is_active = models.BooleanField(
        verbose_name=_("Is project active"), default=True)
    slug = models.SlugField()

    def __unicode__(self):
        return "%s" % self.name

    class Meta:
        permissions = (
            ("view_project", "Can view projects"),
        )
        ordering = ['name']


class DatabaseAliveManager(models.Manager):

    """manager for returning """

    def get_query_set(self):
        return Database.objects.filter(is_in_quarantine=False)


class Database(BaseModel):
    DEAD = 0
    ALIVE = 1
    INITIALIZING = 2

    DB_STATUS = (
        (DEAD, 'Dead'),
        (ALIVE, 'Alive'),
        (INITIALIZING, 'Initializing')
    )


    name = models.CharField(verbose_name=_("Database name"), max_length=100)
    databaseinfra = models.ForeignKey(DatabaseInfra, related_name="databases", on_delete=models.PROTECT)
    project = models.ForeignKey(Project, related_name="databases", on_delete=models.PROTECT, null=True, blank=True)
    team = models.ForeignKey(Team, related_name="databases",
                             help_text=_("Team that is accountable for the database"),
                             null=True,
                             blank=True)
    is_in_quarantine = models.BooleanField(verbose_name=_("Is database in quarantine?"), default=False)
    quarantine_dt = models.DateField(verbose_name=_("Quarantine date"), null=True, blank=True, editable=False)
    description = models.TextField(verbose_name=_("Description"), null=True, blank=True)

    objects = models.Manager()  # The default manager.
    alive = DatabaseAliveManager()  # The alive dbs specific manager.

    quarantine_time = Configuration.get_by_name_as_int('quarantine_retention_days')
    status = models.IntegerField(choices=DB_STATUS,
                                 default=2)
    used_size_in_bytes = models.FloatField(default=0.0)
    environment = models.ForeignKey(Environment, related_name="databases", on_delete=models.PROTECT)

    def __unicode__(self):
        return u"%s" % self.name


    class Meta:
        permissions = (
            ("can_manage_quarantine_databases", "Can manage databases in quarantine"),
            ("view_database", "Can view databases"),
        )
        unique_together = (
            ('name', 'databaseinfra'),
        )

        ordering = ('name', )


    @property
    def infra(self):
        """ Total size of database (in bytes) """
        return self.databaseinfra


    @property
    def engine_type(self):
        return self.infra.engine_name


    @property
    def plan(self):
        return self.databaseinfra and self.databaseinfra.plan


    # @property
    # def environment(self):
    #     return self.databaseinfra and self.databaseinfra.environment


    def delete(self, *args, **kwargs):
        if self.is_in_quarantine:
            LOG.warning("Database %s is in quarantine and will be removed" % self.name)
            for credential in self.credentials.all():
                instance = factory_for(self.databaseinfra)
                instance.remove_user(credential)
            super(Database, self).delete(*args, **kwargs)  # Call the "real" delete() method.

        else:
            LOG.warning("Putting database %s in quarantine" % self.name)
            self.is_in_quarantine = True
            self.save()
            if self.credentials.exists():
                    for credential in self.credentials.all():
                        new_password = make_db_random_password()
                        new_credential = Credential.objects.get(pk=credential.id)
                        new_credential.password = new_password
                        new_credential.save()

                        instance = factory_for(self.databaseinfra)
                        instance.update_user(new_credential)


    def clean(self):
        # slugify name
        if not self.pk:
            # new database
            self.name = slugify(self.name)

        if self.name in self.__get_database_reserved_names():
            raise ValidationError(_("%s is a reserved database name" % self.name))


    def automatic_create_first_credential(self):
        LOG.info("creating new credential for database %s" % self.name)
        user = Credential.USER_PATTERN % self.name
        credential = Credential.create_new_credential(user, self)
        return credential


    @classmethod
    def provision(cls, name, databaseinfra):
        if not isinstance(databaseinfra, DatabaseInfra):
                raise ValidationError('Invalid databaseinfra type %s - %s' % (type(databaseinfra), databaseinfra))

        database = Database()
        database.databaseinfra = databaseinfra
        database.environment = databaseinfra.environment
        database.name = name
        database.full_clean()
        database.save()
        database = Database.objects.get(pk=database.pk)
        return database


    def __get_database_reserved_names(self):
        return getattr(self.driver, 'RESERVED_DATABASES_NAME', [])


    @property
    def driver(self):
        if self.databaseinfra_id is not None:
            return self.databaseinfra.get_driver()

    def get_endpoint(self):
        return self.driver.get_connection(database=self)

    def get_endpoint_dns(self):
        return self.driver.get_connection_dns(database=self)

    def get_log_url(self):

        if Configuration.get_by_name_as_int('laas_integration') != 1:
            return ""

        if self.databaseinfra.plan.provider == Plan.PREPROVISIONED:
            return ""

        from util import get_credentials_for
        from util.laas import get_group_name
        from dbaas_credentials.models import CredentialType

        credential = get_credentials_for(environment=self.environment, credential_type=CredentialType.LOGNIT)
        url = "%s%s" % (credential.endpoint, get_group_name(self))
        return "%s" % (url)

    def get_is_preprovisioned(self):
        if self.databaseinfra.plan.provider == Plan.PREPROVISIONED:
            return True

    endpoint = property(get_endpoint)
    endpoint_dns = property(get_endpoint_dns)

    @cached_property
    def database_status(self):
        try:
            info = self.databaseinfra.get_info()
            if info is None:
                return None
            database_status = info.get_database_status(self.name)

            if database_status is None:
                # try get without cache
                info = self.databaseinfra.get_info(force_refresh=True)
                database_status = info.get_database_status(self.name)
        except ConnectionError, e:
            LOG.error(
                "ConnectionError calling database_status for database %s: %s" % (self, e))
            database_status = DatabaseStatus(self)

        return database_status

    def get_cloudstack_service_offering(self):
        LOG.info("Get offering")
        try:
            offer_name = self.databaseinfra.cs_dbinfra_offering.get().offering.name
        except Exception, e:
            LOG.info("Oops...{}".format(e))
            offer_name = None

        return offer_name

    offering = property(get_cloudstack_service_offering)

    @property
    def total_size(self):
        """ Total size of database (in bytes) """
        return self.databaseinfra.per_database_size_bytes

    @property
    def total_size_in_mb(self):
        """ Total size of database (in bytes) """
        return self.databaseinfra.per_database_size_bytes * MB_FACTOR

    @property
    def total_size_in_gb(self):
        """ Total size of database (in bytes) """
        return self.databaseinfra.per_database_size_bytes * GB_FACTOR

    @property
    def used_size_in_mb(self):
        """ Used size of database (in bytes) """
        return self.used_size_in_bytes * MB_FACTOR

    @property
    def used_size_in_gb(self):
        """ Used size of database (in bytes) """
        return self.used_size_in_bytes * GB_FACTOR

    @property
    def capacity(self):
        """ Float number about used capacity """
        if self.status:
            return round((1.0 * self.used_size_in_bytes / self.total_size) if self.total_size else 0, 2)

    @classmethod
    def purge_quarantine(self):
        quarantine_time = Configuration.get_by_name_as_int(
            'quarantine_retention_days')
        quarantine_time_dt = date.today() - timedelta(days=quarantine_time)
        databases = Database.objects.filter(
            is_in_quarantine=True, quarantine_dt__lte=quarantine_time_dt)
        for database in databases:
            database.delete()
            LOG.info("The database %s was deleted, because it was set to quarentine %d days ago" % (
                database.name, quarantine_time))

    @classmethod
    @transaction.commit_manually
    def clone(cls, database, clone_name, plan, environment, user):
        # try:
        #    cloned_database = Database.objects.get(pk=database.pk)
        #    cloned_database.name = clone_name
        #    cloned_database.pk = None
        #    if not databaseinfra:
        #        cloned_database.databaseinfra = DatabaseInfra.best_for(database.plan, database.environment)
        #    else:
        #        cloned_database.databaseinfra = databaseinfra
        #    cloned_database.save()
        # except:
        #    transaction.rollback()
        #    raise
        # else:
        #    transaction.commit()
        # call task
        #    from notification.tasks import clone_database
        #    result = clone_database.delay(database, cloned_database, user=user)
        from notification.tasks import clone_database

        result = clone_database.delay(database, clone_name, plan, environment, user=user)

    @classmethod
    @transaction.commit_manually
    def resize(cls, database, cloudstackpack, user):
        from notification.tasks import resize_database

        result = resize_database.delay(database, cloudstackpack, user)

    def get_metrics_url(self):
        return "/admin/logical/database/{}/metrics/".format(self.id)

    def get_resize_url(self):
        return "/admin/logical/database/{}/resize/".format(self.id)

    def get_lognit_url(self):
        return "/admin/logical/database/{}/lognit/".format(self.id)

    def get_cloudstack_service_offering_id(self):
        LOG.info("Get offering")
        try:
            offer_id = self.databaseinfra.cs_dbinfra_offering.get().offering.serviceofferingid
        except Exception, e:
            LOG.info("Oops...{}".format(e))
            offer_id = None

        return offer_id

    offering_id = property(get_cloudstack_service_offering_id)


class Credential(BaseModel):
    USER_PATTERN = "u_%s"
    USER_MAXIMUM_LENGTH_NAME = 16

    user = models.CharField(verbose_name=_("User name"), max_length=100)
    password = EncryptedCharField(
        verbose_name=_("User password"), max_length=255)
    database = models.ForeignKey(Database, related_name="credentials")

    def __unicode__(self):
        return u"%s" % self.user

    class Meta:
        permissions = (
            ("view_credential", "Can view credentials"),
        )
        unique_together = (
            ('user', 'database'),
        )
        ordering = ('database', 'user',)

    def clean(self):
        if len(self.user) > self.USER_MAXIMUM_LENGTH_NAME:
            raise ValidationError(_("%s is too long" % self.user))

    @cached_property
    def driver(self):
        return self.database.databaseinfra.get_driver()

    def reset_password(self):
        """ Reset credential password to a new random password """
        self.password = make_db_random_password()
        self.driver.update_user(self)
        self.save()

    @classmethod
    def create_new_credential(cls, user, database):
        credential = Credential()
        credential.database = database
        credential.user = user[:cls.USER_MAXIMUM_LENGTH_NAME]
        credential.user = slugify(credential.user)
        credential.password = make_db_random_password()
        credential.full_clean()
        credential.driver.create_user(credential)
        credential.save()
        return credential

    def delete(self, *args, **kwargs):
        self.driver.remove_user(self)
        LOG.info('User removed from driver')
        super(Credential, self).delete(*args, **kwargs)


#
# SIGNALS
#
@receiver(pre_delete, sender=Database)
def database_pre_delete(sender, **kwargs):
    """
database pre delete signal. Removes database from the engine
"""
    database = kwargs.get("instance")
    LOG.debug("database pre-delete triggered")
    engine = factory_for(database.databaseinfra)
    engine.remove_database(database)


@receiver(post_save, sender=Database)
def database_post_save(sender, **kwargs):
    """
database post save signal. Creates the database in the driver and creates a new credential.
"""
    database = kwargs.get("instance")
    is_new = kwargs.get("created")
    LOG.debug("database post-save triggered")
    if is_new and database.engine_type != 'redis':
        LOG.info("a new database (%s) were created... provision it in the engine" % (database.name))
        engine = factory_for(database.databaseinfra)
        engine.create_database(database)
        database.automatic_create_first_credential()


@receiver(pre_save, sender=Database)
def database_pre_save(sender, **kwargs):
    database = kwargs.get('instance')
    if database.is_in_quarantine:
        if database.quarantine_dt is None:
            database.quarantine_dt = datetime.datetime.now().date()
    else:
        database.quarantine_dt = None

    if database.id:
        saved_object = Database.objects.get(id=database.id)
        if database.name != saved_object.name:
            raise AttributeError(_("Attribute name cannot be edited"))
    else:
        # new database
        LOG.debug("slugfying database's name for %s" % database.name)
        database.name = slugify(database.name)


@receiver(pre_save, sender=Credential)
def credential_pre_save(sender, **kwargs):
    credential = kwargs.get('instance')

    if credential.id:
        saved_object = Credential.objects.get(id=credential.id)
        if credential.user != saved_object.user:
            raise AttributeError(_("Attribute user cannot be edited"))

        if credential.database != saved_object.database:
            raise AttributeError(_("Attribute database cannot be edited"))


@receiver(pre_save, sender=Project)
def project_pre_save(sender, **kwargs):
    instance = kwargs.get('instance')
    instance.slug = slugify(instance.name)


class NoDatabaseInfraCapacity(Exception):

    """ There isn't databaseinfra capable to support a new database with this plan """
    pass


simple_audit.register(Project, Database, Credential)
