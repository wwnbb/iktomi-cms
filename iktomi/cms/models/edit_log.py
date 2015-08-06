# -*- coding: utf-8 -*-
from sqlalchemy import Column, ForeignKey, Integer, PickleType, DateTime, \
        String, desc
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.orm import relationship
from datetime import datetime
from iktomi.cms.item_lock import ItemLock
from iktomi.utils import cached_property
#from webob.multidict import MultiDict
from .base import register_model
import json

__all__ = ['EditLog']


def make_diff(field1, field2, changed=False):
    # XXX move to proper place
    widget = json.dumps(field1.widget.render())
    label = field1.label or field1.name
    before = lambda: field1.get_json_value()
    name = field2.input_name
    after = lambda: field2.get_json_value()

    return dict(label=label,
                widget=widget,
                name=name,
                before=before,
                after=after,
                changed=changed)


# XXX MySQL-specific type. How to resulve this?
class MediumPickleType(PickleType):

    impl = MEDIUMBLOB


@register_model('BaseModel')
def EditLogAdminUser(models):

    admin_id  = Column(ForeignKey(models.AdminUser.id, ondelete="CASCADE"),
                       primary_key=True)
    log_id  = Column(Integer, ForeignKey(models.EditLog.id, ondelete="CASCADE"),
                     primary_key=True)


@register_model('BaseModel')
def EditLog(models):

    id = Column(Integer, primary_key=True)
    stream_name = Column(String(100), nullable=False, default='')
    type = Column(String(50), nullable=False, default='edit')
    # object id can be string, so we use string here
    object_id = Column(String(100), nullable=True)
    global_id = Column(String(100), nullable=False, default='')
    edit_session = Column(String(50), nullable=False, default='')

    before = Column(MediumPickleType, default=list)
    after = Column(MediumPickleType, default=list)
    #diff = Column(Html(MediumText), nullable=True)

    creation_time = Column(DateTime, default=datetime.now, nullable=False)
    update_time = Column(DateTime, default=datetime.now, nullable=False)

    # there is a case when log item can be authored by multiple users:
    # if draft has been made by one user and than corrected and saved by other
    users = relationship(models.AdminUser,
                          secondary=models.EditLogAdminUser.__table__)

    @cached_property
    def data_changed(self):
        return self.before != self.after

    @classmethod
    def query_for_item(cls, db, item):
        ''''''
        global_id = ItemLock.item_global_id(item)
        return db.query(cls).filter_by(global_id=global_id)

    @classmethod
    def last_for_item(cls, db, stream_name, item, user, edit_session):
        ''''''
        # XXX filter by update_time?
        log = db.query(cls)\
                .filter(cls.users.contains(user))\
                .filter_by(stream_name=stream_name,
                           object_id=item.id)\
                .order_by(desc(cls.update_time))\
                .first()
        if log and log.edit_session == edit_session and log.type=='edit':
            return log

    __mapper_args__ = {'order_by': desc(update_time)}
    #Index('draft_index', stream_name, object_id)

