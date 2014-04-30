# -*- coding: utf-8 -*-
import inspect
import json
from datetime import datetime
from webob.exc import HTTPNotFound, HTTPForbidden, HTTPOk
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import class_mapper, RelationshipProperty
from sqlalchemy.orm.util import identity_key

from iktomi.utils import cached_property
from iktomi import web
from iktomi.utils.paginator import ModelPaginator, FancyPageRange
from iktomi.utils.mdict import MultiDict
from iktomi.web.url_converters import Integer as IntegerConv, \
                        Converter as BaseConv, ConvertError
from iktomi.forms import convs


from .item_lock import lock_template_data, prepare_lock_data
from .item_lock import ModelLockError, ItemLock
from .stream_actions import StreamAction
from .flashmessages import flash


def see_other(location):
    return web.Response(json.dumps({'location': location}),
                        content_type="application/json")

def insure_is_xhr(env):
    if (env.request.method in ('GET', 'HEAD') and \
           not env.request.is_xhr and \
           not '__ajax' in env.request.GET):
        raise HTTPOk(body=env.render_to_string('layout.html', {}))


class NoneIntConv(IntegerConv):

    name = 'noneint'
    regex = BaseConv.regex

    def to_python(self, value, **kwargs):
        if value == '+':
            return None
        try:
            value = int(value)
        except ValueError:
            raise ConvertError(self, value)
        else:
            return value

    def to_url(self, value):
        if value is None:
            return '+'
        return str(value)


class StreamListHandler(StreamAction):

    for_item = False
    item_lock = False
    display = False

    @cached_property
    def template_name(self):
        return self.stream.stream_template_name

    @cached_property
    def base_template(self):
        return getattr(self.stream, 'list_base_template', 'stream_base.html')

    @property
    def app(self):
        return web.match() | self

    def list_handler(self, env, data):
        insure_is_xhr(env)

        stream = self.stream
        stream.insure_has_permission(env, 'x')
        read_allowed = stream.has_permission(env, 'r')
        template_name = stream.row_template_name
        template_data = stream.template_data
        no_layout = ('__no_layout' in env.request.GET)

        def item_row(item, list_fields=None, url='#', row_cls='', **kw):
            return env.render_to_string(template_name, dict(template_data,
                                             item=item,
                                             list_fields=list_fields,
                                             url=url,
                                             row_cls=row_cls,
                                             read_allowed=read_allowed,
                                             stream=stream,
                                             **kw))

        data = dict(self.prepare_data(env, data),
                    is_popup=('__popup' in env.request.GET),
                    no_layout=no_layout,
                    menu=stream.module_name,
                    title=stream.title,
                    item_row=item_row,
                    live_search=stream.live_search,
                    base_template=self.base_template)

        data = stream.process_item_template_data(env, data)

        return env.render_to_response(self.template_name, data)
    __call__ = list_handler

    def prepare_data(self, env, data):
        '''View for index page.'''
        stream = self.stream
        request = env.request

        # XXX Check permissions and return 403 if no access.
        # XXX Handle POST to edit/delete here.
        query = stream.item_query(env)
        filter_form = stream.get_filter_form(env)

        # Note: errors are displayed, but ignored in code.
        filter_form.accept(request.GET)
        filter_data = filter_form.get_data()
        query = filter_form.filter(query)

        query = stream.order(query)

        paginator = ModelPaginator(request, query,
                                   impl=FancyPageRange(),
                                   limit=getattr(stream.config, 'limit', None),
                                   url=stream.url_for(env).qs_set(filter_data))

        def item_url(item=None):
            item_id = item.id if item is not None else None
            return stream.url_for(env, 'item', item=item_id).qs_set(
                                                                filter_data)

        try:
            paginator.items = stream.config.modify_items(paginator.items)
        except AttributeError:
            pass


        result = dict(stream.template_data,
                      paginator=paginator,
                      stream=stream,
                      item_url=item_url,
                      list_fields=stream.list_fields,
                      title=stream.config.title,
                      filter_form=filter_form,
                      allow_add=stream.has_permission(env, 'c'),
                      repr=repr)
        result.update(self.list_form_data(env, paginator, filter_data))
        result = stream.process_list_template_data(env, result)
        return result

    def list_form_data(self, env, paginator, filter_data):
        if self.stream.ListItemForm and not filter_data and \
                self.stream.list_edit_action.save_allowed(env):
            return {'list_item_form':self.stream.ListItemForm.for_items(
                                            env, paginator.items)}
        return {}



class PrepareItemHandler(web.WebHandler):
    """ Helper handler to fetch item by id field.
    `data` in handler should have `item` attr containts item id.
    """
    # XXX: Add support for fetching by pk, not only `id` field
    #       (use query.get(pk_val) )

    def __init__(self, action):
        self.action = action

    def retrieve_item(self, env, item):
        return self.action.stream.item_query(env).filter_by(id=item).first()

    def prepare_item_handler(self, env, data):
        '''Item actions dispatcher'''
        insure_is_xhr(env)

        stream = self.action.stream
        stream.insure_has_permission(env, 'r')

        data.filter_form = stream.get_filter_form(env)
        # Note: errors are displayed, but ignored in code.
        data.filter_form.accept(env.request.GET)

        if data.item is not None:
            data.item = self.retrieve_item(env, data.item)
            if data.item is None:
                raise HTTPNotFound
        elif not self.action.allowed_for_new:
            flash(env, u'Действие «%s» недоступно для нового объекта' %
                  (self.action.title,),
                  'failure')
            item_url = stream.url_for(env, 'item')(
                data.filter_form.get_data())
            return see_other(item_url)
        prepare_lock_data(env, data, data.item if self.action.item_lock else None)
        return self.next_handler(env, data)
    __call__ = prepare_item_handler


class EditItemHandler(StreamAction):

    action = 'item'
    item_lock = True
    allowed_for_new = True
    display = False
    title = u'Редактировать/создать'
    PrepareItemHandler = PrepareItemHandler

    @property
    def app_prefix(self):
        return web.prefix('/<noneint:item>', name='item',
                          convs={'noneint': NoneIntConv})
    @property
    def app(self):
        return self.app_prefix | self.PrepareItemHandler(self) | web.cases(
                web.match('', '') | self,
                web.match('/autosave', 'autosave') | \
                        web.method('POST', strict=True) | self.autosave
            )

    def create_allowed(self, env):
        return self.stream.has_permission(env, 'c')

    def save_allowed(self, env, item=None):
        return self.stream.has_permission(env, 'w')

    def delete_allowed(self, env, item=None):
        return self.stream.has_permission(env, 'd')

    def get_item_template(self, env, item):
        return self.stream.item_template_name

    def _clean_item_data(self, stream, env, item):
        form_cls = stream.config.ItemForm
        form = form_cls.load_initial(env, item, initial={}, permissions='r', for_diff=True)
        raw_data = form.raw_data.items()
        unic = lambda s: (s if type(s) in (unicode, bytes) else unicode(s))
        return [(unic(k), unic(v)) for k, v in raw_data]

    def get_item_form(self, stream, env, item, initial, draft=None):
        save_allowed = self.create_allowed(env) \
                if (item is None or item.id is None) else \
                self.save_allowed(env, item)
        form_kw = {}
        if not save_allowed:
            form_kw['permissions'] = 'r'

        form_cls = stream.config.ItemForm
        form = form_cls.load_initial(env, item, initial=initial, **form_kw)
        form.model = self.stream.get_model(env)
        form.draft = draft
        if draft is not None:
            raw_data = MultiDict(draft.data)
            form.accept(raw_data) # XXX
        return form

    def save_item(self, env, filter_form, form, item, draft, autosave):
        form.update_instance(item)

        if item not in env.db:
            env.db.add(item)
        if draft is not None:
            env.db.delete(draft)

        self.stream.commit_item_transaction(env, item, silent=autosave)
        if hasattr(self, 'post_create'):
            self.post_create(item)

        item_url = self.stream.url_for(env, 'item', item=item.id)\
                              .qs_set(filter_form.get_data())
        autosave_url = self.stream.url_for(env, 'item.autosave', item=item.id)\
                                  .qs_set(filter_form.get_data())
        return item, item_url, autosave_url

    def edit_item_handler(self, env, data):
        '''View for item page.'''

        item, lock_message, filter_form = \
            data.item, data.lock_message, data.filter_form
        stream = self.stream
        request = env.request

        initial = filter_form.defaults()
        stream_url = stream.url_for(env).qs_set(filter_form.get_data())
        create_allowed = save_allowed = self.create_allowed(env)
        delete_allowed = False
        success = False

        if item is None:
            if not create_allowed:
                raise HTTPForbidden
            # We must pass initial data to allow creation of models with
            # inheritance.
            item = stream.get_model(env)(**initial)
            # Don't add it to session since we don't know yet whether it should
            # be saved (there can be errors in form).
        else:
            save_allowed = self.save_allowed(env, item)
            delete_allowed = self.delete_allowed(env, item)


        autosave_allowed = save_allowed and \
                           getattr(env, 'draft_form_model', None) and \
                           stream.autosave
        autosave = autosave_allowed and getattr(data, 'autosave', False)
        if autosave_allowed:
            DraftForm = env.draft_form_model
            draft = DraftForm.get_for_item(env.db, stream.uid(env),
                                           item, env.user)
            has_draft = bool(draft)
        elif getattr(data, 'autosave', False):
            raise HTTPForbidden
        else:
            draft = None
            has_draft = False

        if item.id is None \
                and not getattr(data, 'autosave', False) \
                and 'force_draft' not in env.request.GET:
            draft = None

        form = self.get_item_form(stream, env, item, initial, draft)
        EditLog = getattr(env, 'edit_log_model', None)
        log_enabled = (item.id is not None and 
                       EditLog is not None and stream.edit_log)

        if request.method == 'POST':
            if not save_allowed:
                raise HTTPForbidden

            log = None
            if log_enabled:
                log = EditLog.last_for_item(
                        env.db, stream.uid(env), item, 
                        env.user, data.edit_session)
                if log is None:
                    before = self._clean_item_data(stream, env, item)
                    log = EditLog(stream_name=stream.uid(env),
                                  type="edit",
                                  object_id=item.id,
                                  global_id=ItemLock.item_global_id(item),
                                  edit_session=data.edit_session,
                                  users=[env.user],
                                  before=before)
                if draft is not None:
                    log.users = list(set(log.users + draft.users))

            accepted = form.accept(request.POST)
            if accepted and not lock_message:
                need_lock = item.id is None and self.item_lock and autosave
                item, item_url, autosave_url = \
                        self.save_item(env, filter_form, form,
                                       item, draft, autosave)

                if log is not None:
                    env.db.commit()
                    # do not write form.raw_data directly, because it can be
                    # different for same data stored in db.
                    # Assume form raw data generated from item as canonical
                    # representation
                    log.after = self._clean_item_data(stream, env, item)
                    if log.after != log.before:
                        log.update_time = datetime.now()
                        env.db.add(log)
                        env.db.commit()

                result = {'success': True,
                          'item_id': item.id,
                          'autosave_url': autosave_url,
                          'item_url': item_url}
                if need_lock:
                    try:
                        gid = ItemLock.item_global_id(item)
                        lock = env.item_lock.create(gid)
                    except ModelLockError, e:
                        flash(env, unicode(e))
                    else:
                        result = dict(result,
                            edit_session=lock,
                            status='captured',
                            global_id=gid)
                return env.json(result)
            elif lock_message and autosave:
                stream.rollback_due_lock_lost(env, item)
                return env.json({'success': False,
                                 'error': 'item_lock',
                                 'lock_message': lock_message})
            elif lock_message:
                stream.rollback_due_lock_lost(env, item)
            elif autosave:
                stream.rollback_due_form_errors(env, item, silent=True)
                if draft is None:
                    draft = DraftForm(stream_name=stream.uid(env),
                                      object_id=item.id)
                    env.db.add(draft)
                draft.data = form.raw_data.items()

                if not env.user in draft.users:
                    draft.users.append(env.user)
                draft.update_time = datetime.now()
                env.db.commit()
                return env.json({'success': False,
                                 'error': 'draft',
                                 'draft_id': draft.id,
                                 'errors': form.errors,
                                 })
            else:
                stream.rollback_due_form_errors(env, item, silent=autosave)

        template_data = dict(filter_form=filter_form,
                             success=success,
                             form=form,
                             roles=env.user.roles,
                             item=item,
                             draft=draft,
                             has_draft=has_draft,
                             stream=stream,
                             stream_title=stream.config.title,
                             title=unicode(item),
                             log_enabled=log_enabled,
                             submit_url=stream.url_for(env, 'item',
                                                    item=item.id).qs_set(
                                                        filter_form.get_data()),

                             menu=stream.module_name,
                             stream_url=stream_url,
                             actions=[x for x in stream.actions 
                                      if x.for_item and x.is_visible(env, item)],
                             item_buttons=stream.buttons,
                             list_allowed=self.stream.has_permission(env, 'x'),
                             autosave_allowed=autosave_allowed,
                             create_allowed=create_allowed,
                             save_allowed=save_allowed,
                             delete_allowed=delete_allowed,
                             is_popup=('__popup' in request.GET))
        if self.item_lock:
            template_data.update(lock_template_data(env, data, item))

        template_data = stream.process_item_template_data(env, template_data)
        template_data = self.process_item_template_data(env, template_data)

        return env.render_to_response(self.get_item_template(env, item),
                                      template_data)
    __call__ = edit_item_handler


    def autosave(self, env, data):
        data.autosave = True
        return self(env, data)

    def process_item_template_data(self, env, template_data):
        '''Preprocessor for template variables.
           Can be overriden by descedant classes.'''
        return template_data


def _iter_subclasses(cls, _seen=None):
    if _seen is None: _seen = set()
    for sub in cls.__subclasses__():
        if sub not in _seen:
            _seen.add(sub)
            yield sub
            for sub in _iter_subclasses(sub, _seen):
                yield sub


class _ReferrersAction(StreamAction):

    def _list_referers(self, env, obj, limit=50, exclude=None):
        if exclude is None:
            exclude = set()
        result = {}
        for cls, props in self._get_referers(env, obj).items():
            for prop, (count, query) in props.items():
                left = limit - len(result)
                if left <= 0:
                    return result
                indirect = []
                for ref in query[:left]:
                    if ref == obj or ref in exclude or ref in result:
                        continue
                    # XXX this is not optimal way to get edit url
                    for stream in env.streams.values():
                        url = stream.get_edit_url(env, ref)
                        if url is not None:
                            result[ref] = url
                            break
                    else:
                        indirect.append(ref)
                assert obj not in result
                for ref in indirect:
                    left = limit - len(result)
                    if left <= 0:
                        return result
                    indirect_exclude = exclude | set([obj]) | set(result) | \
                        set(indirect)
                    indirect_referers = self._list_referers(
                        env, ref, limit=left,
                        exclude=indirect_exclude)
                    # XXX The following creates false warnings for DocLink
                    # objects that are "private" to current doc.
                    #if not (exclude or indirect_referers):
                    #    # We have to insure that there is a link for each top
                    #    # level referer (that's why exclude is checked).
                    #    logger.warning("Can't find editable object for %s",
                    #                   ref)
                    result.update(indirect_referers)
                    assert obj not in result
        return result

    def _get_all_classes(self, model):
        metadata = model.metadata
        models = [x for x in inspect.getmro(model)
                  if hasattr(x, 'metadata') and x.metadata is metadata]
        set_models = set(models)
        base_models = set(models)
        for m in models:
            if set(m.__bases__) & set_models:
                base_models.remove(m)

        models = set()
        for m in base_models:
            models |= set([x for x in _iter_subclasses(m)
                           if not x.__dict__.get('__abstract__', False)])
        return models

    def _get_referers(self, env, item):
        '''Returns a dictionary mapping referer model class to query of all
        objects of this class refering to current object.'''
        # XXX not implemented
        cls, ident = identity_key(instance=item)
        result = {}
        for other_class in self._get_all_classes(cls):
            queries = {}
            for prop in class_mapper(other_class).iterate_properties:
                if not (isinstance(prop, RelationshipProperty) and \
                        issubclass(cls, prop.mapper.class_)):
                    continue
                query = env.db.query(prop.parent)
                comp = prop.comparator
                if prop.uselist:
                    query = query.filter(comp.contains(item))
                else:
                    query = query.filter(comp==item)
                count = query.count()
                if count:
                    queries[prop] = (count, query)
            if queries:
                result[other_class] = queries
        return result


class DeleteItemHandler(_ReferrersAction):

    action = 'delete'
    cls = 'delete'
    title = u'Удалить'

    @property
    def app(self):
        return web.match('/<noneint:item>/delete', 'delete',
                         convs={'noneint': NoneIntConv}) | \
            PrepareItemHandler(self) | self

    def is_available(self, env, item):
        return StreamAction.is_available(self, env, item) and \
                self.stream.has_permission(env, 'd')

    def delete_item_handler(self, env, data):
        insure_is_xhr(env)
        item, edit_session, lock_message, filter_form = \
            data.item, data.edit_session, data.lock_message, data.filter_form
        stream = self.stream

        self.insure_is_available(env, item)

        stream_url = stream.url_for(env).qs_set(filter_form.get_data())
        item_url = stream.url_for(env, 'item', item=item.id).qs_set(
                                   filter_form.get_data())
        delete_url = stream.url_for(env, 'delete', item=item.id)\
                           .qs_set(filter_form.get_data())
        if env.request.method == 'POST':
            env.db.delete(item)
            try:
                env.db.commit()
            except IntegrityError:
                env.db.rollback()
                flash(env, u'Невозможно удалить объект (%s) пока на него'\
                      u' ссылаются другие объекты' % (item,),
                      'failure')
                return env.json({'result': 'failure'})
            return env.json({'result': 'success',
                             'location': stream_url})
        data = dict(item=item,
                    item_url=item_url,
                    form_url=delete_url,
                    referers=self._list_referers(env, item),
                    title=u'Удаление объекта «%s»' % item,
                    stream=stream,
                    stream_url=stream_url,
                    menu=stream.module_name)
        return env.render_to_response('delete', data)
    __call__ = delete_item_handler


class GetReferrersHandler(_ReferrersAction):

    mode = 'popup'
    item_lock = False
    accepts_item_form = False
    display = False
    action = 'referrers'
    title = u'Связанные объекты'
    PrepareItemHandler = PrepareItemHandler

    @property
    def app(self):
        return web.match('/<noneint:item>/{}'.format(self.action),
                         self.action,
                         convs={'noneint': NoneIntConv}) | \
            self.PrepareItemHandler(self) | self

    def referrers_handler(self, env, data):
        insure_is_xhr(env)
        data = dict(item=data.item,
                    referers=self._list_referers(env, data.item))
        return env.render_to_response('referrers', data)
    __call__ = referrers_handler

