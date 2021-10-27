import types
from datetime import datetime
from typing import Generic, Tuple, Type, TypeVar

from mongoengine import Document, DoesNotExist, connect
from mongoengine.errors import ValidationError
from mongoengine.fields import DateTimeField, StringField
from mongoengine.queryset.queryset import QuerySet

from datasets_preview_backend.config import MONGO_QUEUE_DATABASE, MONGO_URL

# from typing import Any, Generic, List, Type, TypedDict, TypeVar, Union


# from datasets_preview_backend.exceptions import Status404Error, StatusError
# from datasets_preview_backend.models.dataset import get_dataset

# START monkey patching ### hack ###
# see https://github.com/sbdchd/mongo-types#install
U = TypeVar("U", bound=Document)


def no_op(self, x):  # type: ignore
    return self


QuerySet.__class_getitem__ = types.MethodType(no_op, QuerySet)  # type: ignore


class QuerySetManager(Generic[U]):
    def __get__(self, instance: object, cls: Type[U]) -> QuerySet[U]:
        return QuerySet(cls, cls._get_collection())


# END monkey patching ### hack ###


def connect_to_queue() -> None:
    connect(MONGO_QUEUE_DATABASE, alias="queue", host=MONGO_URL)


# States:
# - waiting: started_at is None and finished_at is None: waiting jobs
# - started: started_at is not None and finished_at is None: started jobs
# - finished: started_at is not None and finished_at is None: started jobs
# For a given dataset_name, any number of finished jobs are allowed, but only 0 or 1
# job for the set of the other states
class Job(Document):
    meta = {"collection": "jobs", "db_alias": "queue"}
    dataset_name = StringField(required=True)
    created_at = DateTimeField(required=True)
    started_at = DateTimeField()
    finished_at = DateTimeField()

    objects = QuerySetManager["Job"]()


# TODO: add priority (webhook: 5, warming: 3, refresh: 1)
# TODO: add status (valid/error/stalled) to the finished jobs
# TODO: limit the size of the queue? remove the oldest if room is needed?
# TODO: how to avoid deadlocks (a worker has taken the job, but never finished)? stalled, hours

# enqueue
# dequeue
# peek
# isfull
# isempty


class EmptyQueue(Exception):
    pass


class JobNotFound(Exception):
    pass


class IncoherentState(Exception):
    pass


class InvalidJobId(Exception):
    pass


def add_job(dataset_name: str) -> None:
    try:
        # Check if a not-finished job already exists
        Job.objects(dataset_name=dataset_name, finished_at=None).get()
    except DoesNotExist:
        Job(dataset_name=dataset_name, created_at=datetime.utcnow()).save()
    # raises MultipleObjectsReturned if more than one entry -> should never occur, we let it raise


def get_job() -> Tuple[str, str]:
    job = Job.objects(started_at=None).order_by("+created_at").first()
    if job is None:
        raise EmptyQueue("no job available")
    if job.finished_at is not None:
        raise IncoherentState("a job with an empty start_at field should not have a finished_at field")
    job.update(started_at=datetime.utcnow())
    return str(job.id), job.dataset_name  # type: ignore


def finish_job(job_id: str) -> None:
    try:
        job = Job.objects(id=job_id, started_at__exists=True, finished_at=None).get()
    except DoesNotExist:
        raise JobNotFound("the job does not exist")
    except ValidationError:
        raise InvalidJobId("the job id is invalid")
    job.update(finished_at=datetime.utcnow())


def clean_database() -> None:
    Job.drop_collection()  # type: ignore


# special reports


def get_jobs_count_with_status(status: str) -> int:
    if status == "waiting":
        return Job.objects(started_at=None).count()
    elif status == "started":
        return Job.objects(started_at__exists=True, finished_at=None).count()
    else:
        # done
        return Job.objects(finished_at__exists=True).count()