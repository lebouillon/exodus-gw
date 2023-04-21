import logging
import queue
import uuid
from datetime import datetime, timedelta

import mock
import pytest

from exodus_gw import models, worker
from exodus_gw.settings import load_settings

NOW_UTC = datetime.utcnow()


def _task(publish_id):
    return models.Task(
        id="8d8a4692-c89b-4b57-840f-b3f0166148d2",
        publish_id=publish_id,
        state="NOT_STARTED",
        deadline=NOW_UTC + timedelta(hours=2),
    )


@mock.patch("exodus_gw.worker.publish.AutoindexEnricher.run")
@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit(
    mock_write_batch,
    mock_get_message,
    mock_autoindex_run,
    fake_publish,
    db,
    caplog,
):
    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )
    # Simulate successful write of items by write_batch.
    mock_write_batch.return_value = True

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've set task state to COMPLETE.
    db.refresh(task)
    assert task.state == "COMPLETE"
    # It should've set publish state to COMMITTED.
    db.refresh(fake_publish)
    assert fake_publish.state == "COMMITTED"

    # It should've called write_batch for items and entry point items.
    mock_write_batch.assert_has_calls(
        calls=[
            mock.call(mock.ANY, delete=False),
            mock.call(mock.ANY, delete=False),
        ]
    )

    # It should've written all items.
    assert "Commit may be incomplete" not in caplog.text

    # It should've invoked the autoindex enricher
    mock_autoindex_run.assert_called_once()


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
def test_commit_expired_task(mock_get_message, fake_publish, db, caplog):
    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )

    # Expire the task
    task.deadline = NOW_UTC - timedelta(hours=5)

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've logged message.
    assert (
        "Task 8d8a4692-c89b-4b57-840f-b3f0166148d2 expired at %s"
        % task.deadline
        in caplog.text
    )
    # It should've set task state to FAILED.
    db.refresh(task)
    assert task.state == "FAILED"
    # It should've set publish state to FAILED.
    db.refresh(fake_publish)
    assert fake_publish.state == "FAILED"


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_write_items_fail(
    mock_write_batch, mock_get_message, fake_publish, db, caplog
):
    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )
    # Simulate failed write of items.
    mock_write_batch.side_effect = [RuntimeError(), None]

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've failed write_batch and recalled to roll back.
    mock_write_batch.assert_has_calls(
        calls=[
            mock.call(mock.ANY, delete=False),
            mock.call(mock.ANY, delete=True),
        ],
        any_order=False,
    )
    # It should've logged messages.
    assert "Exception while submitting batch write(s)" in caplog.text
    assert (
        "Task 8d8a4692-c89b-4b57-840f-b3f0166148d2 encountered an error"
        in caplog.text
    )
    assert "Rolling back 2 item(s) due to error" in caplog.text
    # It should've set task state to FAILED.
    db.refresh(task)
    assert task.state == "FAILED"
    # It should've set publish state to FAILED.
    db.refresh(fake_publish)
    assert fake_publish.state == "FAILED"


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_write_entry_point_items_fail(
    mock_write_batch, mock_get_message, fake_publish, db, caplog
):
    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )
    # Simulate successful write of items, failed write of entry point items
    # and then successful deletion of items.
    mock_write_batch.side_effect = [None, RuntimeError(), None]

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've called write_batch for items, entry point items
    # and then deletion of written items.
    mock_write_batch.assert_has_calls(
        calls=[
            mock.call(mock.ANY, delete=False),
            mock.call(mock.ANY, delete=False),
            mock.call(mock.ANY, delete=True),
        ],
        any_order=False,
    )
    # It should've logged messages.
    assert "Exception while submitting batch write(s)" in caplog.text
    assert "Rolling back 3 item(s) due to error" in caplog.text
    # It should've set task state to FAILED.
    db.refresh(task)
    assert task.state == "FAILED"
    # It should've set publish state to FAILED.
    db.refresh(fake_publish)
    assert fake_publish.state == "FAILED"


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_completed_task(mock_write_batch, mock_get_message, db, caplog):
    # Construct task that would be generated by caller.
    task = _task(publish_id="123e4567-e89b-12d3-a456-426614174000")
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": task.publish_id}
    )

    db.add(task)
    # Simulate prior completion of task.
    task.state = "COMPLETE"
    db.commit()

    worker.commit(task.publish_id, "test", NOW_UTC)

    # It should've logged a warning message.
    assert "Task %s in unexpected state, 'COMPLETE'" % task.id in caplog.text
    # It should not have called write_batch.
    mock_write_batch.assert_not_called()


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_completed_publish(
    mock_write_batch, mock_get_message, fake_publish, db, caplog
):
    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )

    db.add(task)
    db.add(fake_publish)
    # Simulate prior completion of publish.
    fake_publish.state = "COMPLETE"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've logged a warning message.
    assert (
        "Publish %s in unexpected state, 'COMPLETE'" % fake_publish.id
        in caplog.text
    )
    # It should not have called write_batch.
    mock_write_batch.assert_not_called()


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_empty_publish(
    mock_write_batch, mock_get_message, fake_publish, db, caplog
):
    caplog.set_level(logging.DEBUG, "exodus-gw")

    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )

    # Empty the publish.
    fake_publish.items = []

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've logged a message.
    assert "No items to write for publish %s" % fake_publish.id in caplog.text
    # It should've set task state to COMPLETE.
    db.refresh(task)
    assert task.state == "COMPLETE"
    # It should've set publish state to COMMITTED.
    db.refresh(fake_publish)
    assert fake_publish.state == "COMMITTED"
    # It should not have called write_batch.
    mock_write_batch.assert_not_called()


@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_write_queue_unfinished(
    mock_write_batch, mock_get_msg, fake_publish, db, caplog
):
    """It's possible for queues to retain items due to worker errors."""
    caplog.set_level(logging.DEBUG, "exodus-gw")

    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_msg.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )
    mock_write_batch.return_value = None

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    settings = load_settings()
    settings.write_max_workers = 1
    settings.write_queue_timeout = 1
    commit_obj = worker.publish.Commit(
        fake_publish.id, fake_publish.env, NOW_UTC, task.id, settings
    )
    bw = worker.publish._BatchWriter(
        commit_obj.dynamodb,
        settings,
        len(fake_publish.items),
        "test write items",
    )
    # Simulate worker issue preventing write_batches from executing and
    # getting items from the queue.
    bw.write_batches = mock.MagicMock()

    with mock.patch("exodus_gw.worker.publish.Commit") as patched_commit:
        patched_commit.return_value = commit_obj
        with mock.patch("exodus_gw.worker.publish._BatchWriter") as patched_bw:
            patched_bw.return_value = bw
            with pytest.raises(RuntimeError):
                worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've logged messages.
    assert "Exception while submitting batch write(s)" in caplog.text
    assert "Commit incomplete, queue not empty" in caplog.text
    assert (
        "Task 8d8a4692-c89b-4b57-840f-b3f0166148d2 encountered an error"
        in caplog.text
    )
    # It should've set task state to FAILED.
    db.refresh(task)
    assert task.state == "FAILED"
    # It should've set publish state to FAILED.
    db.refresh(fake_publish)
    assert fake_publish.state == "FAILED"


@mock.patch("queue.Queue.put")
@mock.patch("exodus_gw.worker.publish.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.publish.DynamoDB.write_batch")
def test_commit_write_queue_full(
    mock_write_batch, mock_get_msg, mock_q_put, fake_publish, db, caplog
):
    """It's possible to hit queue.put timeout
    (e.g., due to slow processing/get), causing queue.Full error.
    """
    caplog.set_level(logging.DEBUG, "exodus-gw")

    # Construct task that would be generated by caller.
    task = _task(fake_publish.id)
    # Construct dramatiq message that would be generated by caller.
    mock_get_msg.return_value = mock.MagicMock(
        message_id=task.id, kwargs={"publish_id": fake_publish.id}
    )
    mock_write_batch.return_value = None

    db.add(fake_publish)
    db.add(task)
    # Caller would've set publish state to COMMITTING.
    fake_publish.state = "COMMITTING"
    db.commit()

    # Simulate some issue causing timeouts after first put.
    mock_q_put.side_effect = [None, queue.Full(), queue.Full(), queue.Full()]

    settings = load_settings()
    settings.write_max_workers = 1
    settings.write_queue_timeout = 1
    commit_obj = worker.publish.Commit(
        fake_publish.id, fake_publish.env, NOW_UTC, task.id, settings
    )

    with mock.patch("exodus_gw.worker.publish.Commit") as patched_commit:
        patched_commit.return_value = commit_obj
        with pytest.raises(queue.Full):
            worker.commit(str(fake_publish.id), fake_publish.env, NOW_UTC)

    # It should've logged messages.
    assert "Exception while submitting batch write(s)" in caplog.text
    assert (
        "Task 8d8a4692-c89b-4b57-840f-b3f0166148d2 encountered an error"
        in caplog.text
    )
    # It should've set task state to FAILED.
    db.refresh(task)
    assert task.state == "FAILED"
    # It should've set publish state to FAILED.
    db.refresh(fake_publish)
    assert fake_publish.state == "FAILED"
