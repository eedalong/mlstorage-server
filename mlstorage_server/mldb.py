import asyncio
from datetime import datetime

import pymongo
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import IndexModel

from mlstorage_server.schema import validate_experiment_doc, validate_experiment_id

__all__ = ['MLDB']


def pop_experiment_id(experiment_doc):
    """Remove "id" and "_id" attribute from `experiment_doc`."""
    experiment_doc.pop('_id', None)
    experiment_doc.pop('id', None)
    return experiment_doc


def to_database_experiment_doc(experiment_doc):
    """Rename "id" to "_id" in `experiment_doc`."""
    if experiment_doc is not None:
        if 'id' in experiment_doc:
            experiment_doc['_id'] = experiment_doc.pop('id')
    return experiment_doc


def from_database_experiment_doc(experiment_doc):
    """Rename "_id" to "id" in `experiment_doc`."""
    if experiment_doc is not None:
        if '_id' in experiment_doc:
            experiment_doc['id'] = experiment_doc.pop('_id')
    return experiment_doc


async def ensure_mongo_indexes(collection, *indexes):
    """
    Ensure the specified MongoDB `collection` has given `indexes`.

    Args:
        collection (AsyncIOMotorCollection): The MongoDB collection.
        \*indexes (list[(str, int)]): List of indices plans
            ``list[(key, direction)]``.

    Notes:
        This method is NOT concurrently safe.
    """
    if indexes:
        created_flag = [False] * len(indexes)
        information = await collection.index_information()
        for _, index_info in information.items():
            for i, index_to_create in enumerate(indexes):
                if index_to_create == index_info['key']:
                    created_flag[i] = True
        index_models = []
        for i, (created, index_to_create) in \
                enumerate(zip(created_flag, indexes)):
            if not created:
                index_models.append(IndexModel(index_to_create))

        if index_models:
            _ = await collection.create_indexes(index_models)


class MLDB(object):
    """
    Storing experiments in MongoDB.

    Every experiment should be stored as a document in MongoDB.
    The basic schema of an experiment document is::

        {
            "id": ID of the experiment,
            "parent_id": ID of the parent experiment, optional,
            "name": experiment name,
            "description": description, optional,
            "tags": list of tags, optional,
            "start_time": time of start, UTC datetime object,
            "stop_time": time of stop, UTC datetime object,
            "heartbeat": time of heart beat, UTC datetime object,
            "status": one of {"RUNNING", "COMPLETED", "FAILED"},
            "error": {
                "message": short error message for describing the failure,
                "traceback": long traceback message, optional
            },
            "exit_code": the exit code of the program,
            "storage_dir": the directory where to store generated files,
            "storage_size": size of the storage directory on disk,
            "exc_info": {
                "hostname": the host name where the program get executed,
                "pid": the process ID,
                "work_dir": the working directory,
                "env": the environmental variables dict
            },
            "webui": {
                Name of the web server: URI of the web server
            },
            "fingerprint": the fingerprint of the program, optional,
            "args": the program arguments,
            "config": the config values passed to the program,
            "default_config": the default config values of the program,
            "result": the result values generated by the program
        }

    Additional fields will be stored in MongoDB as-is.
    """

    def __init__(self, collection):
        """
        Construct a new :class:`MLDB`.

        Args:
            collection (AsyncIOMotorCollection): The MongoDB collection,
                 where to store the experiment documents.
        """
        self._collection = collection

        # flag to indicate whether or not ensure index has been called
        self._indexes_ensured = False

    @property
    def collection(self):
        """
        Get the MongoDB collection.

        Returns:
            AsyncIOMotorCollection: The MongoDB collection object.
        """
        return self._collection

    async def ensure_indexes(self):
        """Ensure the indexes of document fields having been created."""
        if not self._indexes_ensured:
            await ensure_mongo_indexes(
                self.collection,
                [('parent_id', pymongo.ASCENDING)],
                [('name', pymongo.ASCENDING)],
                [('tags', pymongo.ASCENDING)],
                [('status', pymongo.ASCENDING)],
                [('fingerprint', pymongo.ASCENDING)],
                [('args', pymongo.ASCENDING)],
                [('deleted', pymongo.ASCENDING)],
                [('start_time', pymongo.DESCENDING)],
                [('stop_time', pymongo.DESCENDING)],
                [('heartbeat', pymongo.DESCENDING)],
            )
            self._indexes_ensured = True

    async def get(self, id):
        """
        Get an experiment document by `id`.

        Args:
            id (str or ObjectId): ID of the experiment.

        Returns:
            dict or None: The experiment document, or :obj:`None` if the
                experiment does not exist or its deletion flag has been set.
        """
        return from_database_experiment_doc(
            await self.collection.find_one(
             {'_id': validate_experiment_id(id), 'deleted': {'$ne': True}}))

    async def create(self, name, doc_fields=None):
        """
        Create an experiment document.

        Args:
            name (str): Name of the experiment.
            doc_fields: Other fields of the experiment document.
                The following fields will be set by default if absent:
                *  start_time: set to ``datetime.utcnow()``.
                *  heartbeat: set to `start_time`.
                *  status: set to "RUNNING".

        Returns:
            ObjectId: The ID of the inserted document.
        """
        doc_fields = validate_experiment_doc(
            pop_experiment_id(dict(doc_fields or ())))
        doc_fields['name'] = name
        if 'start_time' not in doc_fields:
            doc_fields['start_time'] = datetime.utcnow()
        if 'heartbeat' not in doc_fields:
            doc_fields['heartbeat'] = doc_fields['start_time']
        if 'status' not in doc_fields:
            doc_fields['status'] = 'RUNNING'
        await self.ensure_indexes()
        return (await self.collection.insert_one(doc_fields)).inserted_id

    async def _update(self, id, doc_fields):
        result = await self.collection.update_one(
            {'_id': id, 'deleted': {'$ne': True}},
            {'$set': doc_fields}
        )
        if result.matched_count < 1:
            raise KeyError('Experiment not exist: {!r}'.format(id))

    async def update(self, id, doc_fields):
        """
        Update an experiment document in ``[coll_name].runs``.

        Args:
            id (str or ObjectId): ID of the experiment.
            doc_fields: Fields of the experiment document to be updated.

        Raises:
            KeyError: If the experiment with `id` does not exist.
        """
        id = validate_experiment_id(id)
        doc_fields = validate_experiment_doc(
            pop_experiment_id(dict(doc_fields or ())))
        await self.ensure_indexes()
        if doc_fields:
            return await self._update(id, doc_fields)

    async def set_heartbeat(self, id, doc_fields=None):
        """
        Set the heartbeat time of an experiment.

        Args:
            id (str or ObjectId): ID of the experiment.
            doc_fields: Other fields to be updated, optional.

        Raises:
            KeyError: If the experiment with `id` does not exist.
        """
        doc_fields = validate_experiment_doc(
            pop_experiment_id(dict(doc_fields or ())))
        doc_fields['heartbeat'] = datetime.utcnow()
        await self.ensure_indexes()
        return await self._update(id, doc_fields)

    async def set_finished(self, id, status, doc_fields=None):
        """
        Set the status of an experiment to "COMPLETED" or "FAILED".

        The "stop_time" and the "heartbeat" will be set to current time.

        Args:
            id (str or ObjectId): ID of the experiment.
            status ({"COMPLETED", "FAILED"}): The final experiment status.
            doc_fields: Other fields to be updated, optional.

        Raises:
            KeyError: If the experiment with `id` does not exist.
        """
        if status not in ('COMPLETED', 'FAILED'):
            raise ValueError('Invalid `status`: {!r}'.format(status))
        doc_fields = validate_experiment_doc(
            pop_experiment_id(dict(doc_fields or ())))
        doc_fields['stop_time'] = doc_fields['heartbeat'] = datetime.utcnow()
        doc_fields['status'] = status
        await self.ensure_indexes()
        return await self._update(id, doc_fields)

    async def _mark_delete(self, id):
        ret = []
        result = await self.collection.update_one(
            {'_id': id}, {'$set': {'deleted': True}})
        if result.matched_count > 0:
            ret.append(id)
            async for c in self.collection.find({'parent_id': id}, {'_id': 1}):
                ret.extend(await self._mark_delete(c['_id']))
        return ret

    async def mark_delete(self, id):
        """
        Set the deletion flags of the experiment `id`, and all its children
        experiments, and return all these marked experiments.

        Args:
            id: The experiment ID.

        Returns:
            list[ObjectId]: The experiments marked to be deleted.
        """
        id = validate_experiment_id(id)
        await self.ensure_indexes()
        return await self._mark_delete(id)

    async def complete_deletion(self, id_list):
        """
        Complete the deletion on `id_list`.

        Args:
            id_list (list[ObjectId]): List of experiment documents to be
                actually deleted from MongoDB.

        Returns:
            The actual number of experiments having been deleted.
        """
        async def deletion_task(id):
            return (await self.collection.delete_one({'_id': id})).deleted_count

        id_list = set(validate_experiment_id(i) for i in id_list)
        tasks = [deletion_task(i) for i in id_list]
        await self.ensure_indexes()
        return sum(await asyncio.gather(*tasks))

    async def iter_docs(self, filter=None, skip=None, limit=None,
                        sort_by=None, include_deleted=False):
        """
        Iterate through experiment documents.

        Args:
            filter: The filter for querying experiment documents.
                If `None`, all experiments will be queried.
            skip: Number of documents to skip at front.
            limit: Limiting the returned document by this number.
            sort_by: The sort ordering.  If not specified, will sort
                by the DESCENDING order of "heartbeat".
            include_deleted (bool): Whether or not to include deleted
                documents? (default :obj:`False`)

        Yields:
            The matched documents, in DESCENDING order of "heartbeat".
        """
        # assemble the query
        filter_ = to_database_experiment_doc(
            validate_experiment_doc(dict(filter or ())))
        if not include_deleted:
            filter_['deleted'] = {'$ne': True}
        if sort_by is None:
            sort_by = [('heartbeat', pymongo.DESCENDING)]

        # open the cursor and fetch documents
        cursor = self.collection.find(
            filter_,
            sort=sort_by
        )
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        async for doc in cursor:
            yield from_database_experiment_doc(doc)

    async def fetch_docs(self, filter=None, skip=None, limit=None,
                         sort_by=None, include_deleted=False):
        """
        Fetch experiment documents.

        Args:
            filter: The filter for querying experiment documents.
                If `None`, all experiments will be queried.
            skip: Number of documents to skip at front.
            limit: Limiting the returned document by this number.
            sort_by: The sort ordering.  If not specified, will sort
                by the DESCENDING order of "heartbeat".
            include_deleted (bool): Whether or not to include deleted
                documents? (default :obj:`False`)

        Returns:
            The matched documents list, in DESCENDING order of "heartbeat".
        """
        ret = []
        async for doc in self.iter_docs(filter, skip, limit, sort_by=sort_by,
                                        include_deleted=include_deleted):
            ret.append(doc)
        return ret