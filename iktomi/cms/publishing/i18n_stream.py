# -*- coding: utf-8 -*-
from webob.exc import HTTPNotFound
from iktomi import web
from iktomi.cms.stream_handlers import PrepareItemHandler
from iktomi.unstable.db.sqla.replication import replicate_attributes
from iktomi.cms.publishing.stream import PublishItemHandler, \
        PublishStreamNoState, PublishStream
from iktomi.cms.stream import Stream


class PrepareI18nItemHandler(PrepareItemHandler):

    def __call__(self, env, data):
        # XXX Dirty hack to support object creation
        env.absent_items = True
        return PrepareItemHandler.__call__(self, env, data)


class I18nItemHandler(PublishItemHandler):

    # XXX turn off autosave or make it save to DraftForm only?

    PrepareItemHandler = PrepareI18nItemHandler

    def get_item_form(self, stream, env, item, initial, draft=None):
        if item.state not in (item.ABSENT, item.DELETED):
            return PublishItemHandler.get_item_form(
                    self, stream, env, item, initial, draft)

        # XXX this method looks hacky
        # Get existing language version and fill the form with object reflection
        # to current language model
        for lang in item.models.langs:
            # XXX item.models is not an interface
            if lang == item.models.lang:
                continue
            source_item = item._item_version('admin', lang)
            if source_item and \
                    source_item.state not in (item.ABSENT, item.DELETED):
                break
        else:
            # The item has been deleted on all language versions, creation is
            # not allowed
            raise HTTPNotFound
        # make object reflection, do not add it to db
        fake_item = item.__class__()
        # XXX do not replicate text fields, creation time, etc
        replicate_attributes(source_item, fake_item)
        form = PublishItemHandler.get_item_form(
                self, stream, env, fake_item, initial, draft)
        # XXX hack!
        form.item = item
        return form

    def process_item_template_data(self, env, td):
        item = td['item']
        if item.state in (item.ABSENT, item.DELETED):
            td['title'] = u'Создание языковой версии объекта'
        return PublishItemHandler.process_item_template_data(self, env, td)


class I18nStreamMixin(object):

    langs = (('ru', u'Русский'),
             ('en', u'Английский'),)
    langs_dict = dict(langs)

    list_base_template = 'lang_publish_stream.html'

    def uid(self, env):
        return self.module_name + '.' + env.version + '.' + env.lang

    @property
    def prefix_handler(self):
        @web.request_filter
        def set_models(env, data, nxt):
            #assert data.version in self.versions_dict.keys()
            env.models = getattr(env.models, data.version)
            env.models = getattr(env.models, data.lang)
            env.version = data.version
            env.lang = data.lang
            return nxt(env, data)

        version_prefix = web.prefix('/<any("%s"):version>' % \
                                     ('","'.join(self.versions_dict.keys())))
        lang_prefix = web.prefix('/<any("%s"):lang>' % \
                                     ('","'.join(self.langs_dict.keys())))
        #return version_prefix | set_models | \
        return super(PublishStreamNoState, self).prefix_handler |\
               version_prefix | lang_prefix | set_models

    def url_for(self, env, name=None, **kwargs):
        kwargs.setdefault('version', getattr(env, 'version', self.versions[0][0]))
        kwargs.setdefault('lang', getattr(env, 'lang', self.langs[0][0]))
        return Stream.url_for(self, env, name, **kwargs)


class I18nPublishStreamNoState(I18nStreamMixin, PublishStreamNoState):
    pass


class I18nPublishStream(I18nStreamMixin, PublishStream):

    core_actions = [x for x in PublishStream.core_actions 
                    if x.action != 'item'] + [
        I18nItemHandler(),
    ]
