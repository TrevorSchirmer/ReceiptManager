"""Merchant auto-rules, refund linking, and slash-command behaviour."""

from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    from app.db import init_db

    init_db()
    yield


def _rule(db, **kw):
    from app.models import ParseRule

    defaults = dict(
        name="alerts", enabled=True, priority=100, sender_match="alerts@",
        subject_match="", match_is_regex=False, default_currency="USD",
        body_regex=r"(?P<amount>-?[$\d,.()]+) at (?P<merchant>.+?)(?:\.|$)",
    )
    defaults.update(kw)
    rule = ParseRule(**defaults)
    db.add(rule)
    db.flush()
    return rule


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 500), (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


def _simulate(db, body: str, subject: str = "alert"):
    from app.services.ingest import simulate_email

    return simulate_email(
        db, sender="alerts@example.com", subject=subject, body=body, is_html=False
    )


def test_merchant_rule_files_charge_and_suppresses_notification(env):
    """The whole point: a matched merchant must NOT be announced.

    If it were still announced, the rule would remove none of the noise it
    exists to remove.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job, MerchantRule, TransactionStatus

    with session_scope() as db:
        _rule(db)
        db.add(MerchantRule(pattern="GITHUB", skip_receipt=True, category="Software",
                            note="Recurring subscription"))

    with session_scope() as db:
        tx = _simulate(db, "$21.00 at GITHUB.")
        assert tx is not None
        assert tx.status == TransactionStatus.no_receipt_required
        assert tx.category == "Software"
        assert "Recurring subscription" in (tx.notes or "")

    with session_scope() as db:
        notify_jobs = db.scalar(
            select(func.count(Job.id)).where(Job.kind == "discord.notify")
        )
        assert notify_jobs == 0, "an auto-filed charge must not be announced"


def test_unmatched_merchant_is_still_announced(env):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job, MerchantRule, TransactionStatus

    with session_scope() as db:
        _rule(db)
        db.add(MerchantRule(pattern="GITHUB", skip_receipt=True))

    with session_scope() as db:
        tx = _simulate(db, "$43.21 at AMAZON MARKETPLACE.")
        assert tx.status == TransactionStatus.new

    with session_scope() as db:
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == "discord.notify")) == 1


def test_merchant_rule_excluded_from_the_picker(env):
    """An auto-filed charge must not appear as a candidate for a receipt."""
    from app.db import session_scope
    from app.models import MerchantRule
    from app.services import matching

    with session_scope() as db:
        _rule(db)
        db.add(MerchantRule(pattern="GITHUB", skip_receipt=True))

    with session_scope() as db:
        _simulate(db, "$21.00 at GITHUB.")

    with session_scope() as db:
        assert matching.open_transactions(db) == []


def test_broken_merchant_regex_does_not_break_ingest(env):
    from app.db import session_scope
    from app.models import MerchantRule, TransactionStatus

    with session_scope() as db:
        _rule(db)
        db.add(MerchantRule(pattern="[unclosed", is_regex=True, skip_receipt=True))

    with session_scope() as db:
        tx = _simulate(db, "$43.21 at AMAZON.")
        assert tx is not None, "a bad merchant regex must never lose a charge"
        assert tx.status == TransactionStatus.new


def test_refund_links_to_its_original_charge(env):
    from app.db import session_scope

    with session_scope() as db:
        _rule(db)

    with session_scope() as db:
        original = _simulate(db, "$43.21 at AMAZON.", subject="charge")
        original_code = original.short_code

    with session_scope() as db:
        refund = _simulate(db, "(43.21) at AMAZON.", subject="refund")
        assert refund.amount_minor == -4321
        assert refund.is_refund
        assert refund.refund_of is not None
        assert refund.refund_of.short_code == original_code


def test_refund_without_a_match_is_left_unlinked(env):
    from app.db import session_scope

    with session_scope() as db:
        _rule(db)

    with session_scope() as db:
        refund = _simulate(db, "(99.99) at NEVER SEEN BEFORE.")
        assert refund.is_refund
        assert refund.refund_of_id is None


def test_refund_does_not_claim_an_already_refunded_charge(env):
    """Two refunds of the same amount must not both point at one charge."""
    from app.db import session_scope

    with session_scope() as db:
        _rule(db)

    with session_scope() as db:
        _simulate(db, "$10.00 at SHOP.", subject="c1")
    with session_scope() as db:
        _simulate(db, "$10.00 at SHOP.", subject="c2")
    with session_scope() as db:
        first = _simulate(db, "(10.00) at SHOP.", subject="r1")
        first_target = first.refund_of_id
    with session_scope() as db:
        second = _simulate(db, "(10.00) at SHOP.", subject="r2")
        assert second.refund_of_id is not None
        assert second.refund_of_id != first_target, "both refunds claimed the same charge"


def test_slash_commands_are_registered():
    from app.discordbot.bot import ReceiptBot

    names = sorted(c.name for c in ReceiptBot().tree.get_commands())
    assert names == ["cat", "note", "pending", "search", "silence", "skip", "whoami"]


def test_slash_skip_marks_no_receipt_required(env):
    from app.db import session_scope
    from app.discordbot.commands import _mutate
    from app.models import TransactionStatus

    with session_scope() as db:
        _rule(db)
    with session_scope() as db:
        tx = _simulate(db, "$43.21 at AMAZON.")
        code = tx.short_code

    reply = _mutate(code, "tester", status=TransactionStatus.no_receipt_required)
    assert "✅" in reply

    with session_scope() as db:
        from sqlalchemy import select

        from app.models import Transaction

        tx = db.scalar(select(Transaction).where(Transaction.short_code == code))
        assert tx.status == TransactionStatus.no_receipt_required


def test_slash_mutate_on_unknown_code_is_graceful(env):
    from app.discordbot.commands import _mutate

    assert "No charge" in _mutate("99999", "tester", note="hi")


def test_slash_pending_and_search(env):
    from app.db import session_scope
    from app.discordbot.commands import _list_pending, _search

    with session_scope() as db:
        _rule(db)
    assert "Nothing is awaiting" in _list_pending()

    with session_scope() as db:
        _simulate(db, "$43.21 at AMAZON MARKETPLACE.")

    assert "AMAZON MARKETPLACE" in _list_pending()
    assert "AMAZON MARKETPLACE" in _search("amazon")
    assert "AMAZON MARKETPLACE" in _search("43.21")   # amount search
    assert "No charges match" in _search("zzzznope")


def test_allowlist_blocks_unknown_discord_users(env):
    from app import settings_keys as sk
    from app.db import session_scope
    from app.discordbot.commands import _authorized

    # Empty allowlist accepts anyone (documented default).
    assert _authorized(12345) is True

    with session_scope() as db:
        sk.put(db, sk.DISCORD_ALLOWED_UPLOADERS, "111,222")

    assert _authorized(111) is True
    assert _authorized(333) is False


# --------------------------------------------------------------------------- #
# Deleting a transaction
# --------------------------------------------------------------------------- #

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db
    from app.main import create_app

    init_db()
    c = TestClient(create_app())
    c.__enter__()
    c.post("/setup", data={"password": "correct-horse-battery-staple",
                           "password_confirm": "correct-horse-battery-staple"},
           follow_redirects=False)
    return c


def _token(c):
    import re
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text).group(1)


def _seed(png: bytes):
    """A charge with an email, a stored receipt, and a Discord notify record."""
    from app.db import session_scope
    from app.models import DiscordMessage
    from app.services import storage

    with session_scope() as db:
        _rule(db)
    with session_scope() as db:
        tx = _simulate(db, "$43.21 at AMAZON.")
        tx.notify_message_id = "555000111"
        db.add(tx)
        db.add(DiscordMessage(message_id="555000111", channel_id="c1",
                              direction="out", kind="notify", transaction_id=tx.id))
        db.flush()
        stored = storage.store_receipt_bytes(png, original_filename="r.png")
        from app.models import Attachment
        db.add(Attachment(transaction_id=tx.id, path=stored.rel_path,
                          thumb_path=stored.thumb_rel_path, mime=stored.mime,
                          bytes=stored.bytes, sha256=stored.sha256,
                          original_filename="r.png"))
        return tx.short_code, stored.rel_path


def test_delete_keeps_receipt_files_by_default(tmp_path, monkeypatch):
    """A mis-click must not destroy the receipt image."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Attachment, RawEmail, Transaction
    from app.services import storage

    c = _client(tmp_path, monkeypatch)
    try:
        code, rel = _seed(_png_bytes())
        r = c.post(f"/transactions/{code}/delete", data={"csrf_token": _token(c)},
                   follow_redirects=False)
        assert r.status_code == 303

        with session_scope() as db:
            assert db.scalar(select(func.count(Transaction.id))) == 0
            # The email goes too, so the same message can be re-ingested.
            assert db.scalar(select(func.count(RawEmail.id))) == 0
            att = db.scalar(select(Attachment))
            assert att is not None and att.transaction_id is None, "receipt was not orphaned"
        assert storage.absolute_path(rel).is_file(), "receipt file was destroyed"
    finally:
        c.__exit__(None, None, None)


def test_delete_can_also_remove_the_files(tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Attachment
    from app.services import storage

    c = _client(tmp_path, monkeypatch)
    try:
        code, rel = _seed(_png_bytes())
        c.post(f"/transactions/{code}/delete",
               data={"csrf_token": _token(c), "delete_receipts": "1"},
               follow_redirects=False)

        with session_scope() as db:
            assert db.scalar(select(func.count(Attachment.id))) == 0
        assert not storage.absolute_path(rel).exists()
    finally:
        c.__exit__(None, None, None)


def test_delete_survives_the_discord_message_foreign_key(tmp_path, monkeypatch):
    """discord_messages has no ON DELETE rule; leaving rows would raise."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import DiscordMessage, Job

    c = _client(tmp_path, monkeypatch)
    try:
        code, _ = _seed(_png_bytes())
        r = c.post(f"/transactions/{code}/delete", data={"csrf_token": _token(c)},
                   follow_redirects=False)
        assert r.status_code == 303

        with session_scope() as db:
            assert db.scalar(select(func.count(DiscordMessage.id))) == 0
            # And the channel gets tidied up.
            assert db.scalar(
                select(func.count(Job.id)).where(Job.kind == "discord.delete_message")
            ) == 1
    finally:
        c.__exit__(None, None, None)


def test_orphan_file_can_be_deleted_but_an_attached_one_cannot(tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Attachment
    from app.services import storage

    c = _client(tmp_path, monkeypatch)
    try:
        code, rel = _seed(_png_bytes())
        with session_scope() as db:
            att_id = db.scalar(select(Attachment.id))

        # Still attached: refused.
        c.post(f"/attachments/{att_id}/delete", data={"csrf_token": _token(c)},
               follow_redirects=False)
        with session_scope() as db:
            assert db.scalar(select(func.count(Attachment.id))) == 1

        # Detached, then deleted.
        c.post(f"/attachments/{att_id}/detach", data={"csrf_token": _token(c)},
               follow_redirects=False)
        c.post(f"/attachments/{att_id}/delete", data={"csrf_token": _token(c)},
               follow_redirects=False)
        with session_scope() as db:
            assert db.scalar(select(func.count(Attachment.id))) == 0
        assert not storage.absolute_path(rel).exists()
    finally:
        c.__exit__(None, None, None)


def test_dead_jobs_can_be_requeued(tmp_path, monkeypatch):
    """A dead job must be recoverable once its cause is fixed.

    Retries are exhausted after eight attempts, so without this a notification
    that failed while Discord was misconfigured is never sent — correcting the
    setting does nothing on its own.
    """
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, JobStatus
    from app.services import jobs

    c = _client(tmp_path, monkeypatch)
    try:
        with session_scope() as db:
            job = jobs.enqueue(db, kind="discord.notify", payload={"transaction_id": 1})
            job.status = JobStatus.dead
            job.attempts = 8
            job.last_error = "NotConnected: Discord gateway is not connected"
            db.add(job)

        # Surfaced on the health page with its error, not buried in the database.
        page = c.get("/health").text
        assert "Failed jobs" in page
        assert "Discord gateway is not connected" in page

        r = c.post("/health/retry-dead", data={"csrf_token": _token(c)}, follow_redirects=False)
        assert r.status_code == 303

        with session_scope() as db:
            revived = db.scalar(select(Job))
            assert revived.status == JobStatus.pending
            assert revived.attempts == 0, "attempt counter must reset or it dies immediately"
            assert revived.last_error is None
    finally:
        c.__exit__(None, None, None)


def test_rowid_reuse_does_not_suppress_the_next_notification(tmp_path, monkeypatch):
    """The bug this guards against was silent and total.

    SQLite reuses rowids. Delete a transaction and ingest another, and the new
    one gets the same id — so an idempotency key like `notify:1` left behind by
    the deleted charge's finished job suppressed the new charge's notification
    entirely. No job, no error, nothing in the logs; the charge simply sat in the
    table and was never announced.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job, Transaction, utcnow
    from app.services import jobs

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    from app.db import init_db

    init_db()

    def make(code: str) -> tuple[int, bool]:
        with session_scope() as db:
            tx = Transaction(short_code=code, occurred_at=utcnow(), merchant="X",
                             amount_minor=1, currency="USD")
            db.add(tx)
            db.flush()
            job = jobs.enqueue(db, kind="discord.notify",
                               payload={"transaction_id": tx.id},
                               idempotency_key=f"notify:{tx.id}")
            return tx.id, job is not None

    first_id, queued = make("1000")
    assert queued

    # The real sequence: the notification was sent before the charge was deleted.
    with session_scope() as db:
        from app.models import JobStatus

        job = db.scalar(select(Job))
        job.status = JobStatus.done
        db.add(job)

    with session_scope() as db:
        db.delete(db.get(Transaction, first_id))

    second_id, queued_again = make("1001")
    assert second_id == first_id, "precondition: SQLite reused the rowid"
    assert queued_again, "the new charge's notification was silently suppressed"

    with session_scope() as db:
        pending = db.scalar(
            select(func.count(Job.id)).where(Job.kind == "discord.notify")
        )
        assert pending == 1, "the finished job should be replaced, not duplicated"


def test_outstanding_work_is_still_deduplicated(tmp_path, monkeypatch):
    """Replacing finished jobs must not weaken real duplicate suppression."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job
    from app.services import jobs

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    from app.db import init_db

    init_db()

    with session_scope() as db:
        assert jobs.enqueue(db, kind="discord.notify", payload={}, idempotency_key="k") is not None
        # Still pending: a second enqueue must be refused.
        assert jobs.enqueue(db, kind="discord.notify", payload={}, idempotency_key="k") is None

    with session_scope() as db:
        assert db.scalar(select(func.count(Job.id))) == 1


def test_only_one_place_confirms_a_stored_receipt():
    """A structural guard against the duplicate-confirmation bug returning.

    "receipt stored" was being sent from three places — the inline reply in the
    message handler, the picker's edit, and the finalize job — so a matched
    upload produced two identical messages in the channel.

    Only finalize is entitled to say it: that is the point at which the bytes
    have been verified on disk. Anything earlier is claiming durability it has
    not established.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    # Match on the file, not a line number, so ordinary edits do not break this.
    hits = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        for line in path.read_text().splitlines()
        if "receipt stored" in line and not line.lstrip().startswith("#")
    }
    assert hits == {"discordbot/tasks.py"}, (
        f"'receipt stored' should only be sent from the finalize job; found in: {sorted(hits)}"
    )


# --------------------------------------------------------------------------- #
# Silencing a merchant
# --------------------------------------------------------------------------- #

def test_silencing_files_charges_that_are_already_outstanding(tmp_path, monkeypatch):
    """The retroactive half is the point.

    Silencing a subscription while its charges are already sitting in the channel
    asking for receipts would leave exactly the noise the rule exists to remove.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job, MerchantRule, Transaction, TransactionStatus

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db

    init_db()

    with session_scope() as db:
        _rule(db)
    for i in range(3):
        with session_scope() as db:
            tx = _simulate(db, "$21.00 at GITHUB.", subject=f"c{i}")
            tx.notify_message_id = f"9000{i}"      # already announced
            tx.status = TransactionStatus.notified
            db.add(tx)
    with session_scope() as db:
        _simulate(db, "$43.21 at AMAZON.", subject="other")

    from app.services.ingest import silence_merchant

    with session_scope() as db:
        rule, affected = silence_merchant(db, "GITHUB")
        assert affected == 3

    with session_scope() as db:
        by_merchant = {t.merchant: t.status for t in db.scalars(select(Transaction))}
        assert by_merchant["GITHUB"] == TransactionStatus.no_receipt_required
        assert by_merchant["AMAZON"] == TransactionStatus.new, "an unrelated charge was silenced"

        # The requests already in the channel are withdrawn, not just muted.
        assert db.scalar(
            select(func.count(Job.id)).where(Job.kind == "discord.delete_message")
        ) == 3
        assert db.scalar(select(func.count(MerchantRule.id))) == 1


def test_silencing_leaves_charges_that_already_have_a_receipt(tmp_path, monkeypatch):
    """Not needing a receipt in future is no reason to discard one that arrived."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Attachment, Transaction, TransactionStatus
    from app.services import storage

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db

    init_db()

    with session_scope() as db:
        _rule(db)
    with session_scope() as db:
        tx = _simulate(db, "$21.00 at GITHUB.")
        stored = storage.store_receipt_bytes(_png_bytes(), original_filename="r.png")
        db.add(Attachment(transaction_id=tx.id, path=stored.rel_path, mime=stored.mime,
                          bytes=stored.bytes, sha256=stored.sha256))
        tx.status = TransactionStatus.receipt_attached
        db.add(tx)

    from app.services.ingest import silence_merchant

    with session_scope() as db:
        _rule_obj, affected = silence_merchant(db, "GITHUB")
        assert affected == 0

    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx.status == TransactionStatus.receipt_attached
        assert tx.attachments, "the stored receipt was discarded"


def test_silencing_also_applies_to_future_charges(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import TransactionStatus

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db

    init_db()

    with session_scope() as db:
        _rule(db)
    from app.services.ingest import silence_merchant

    with session_scope() as db:
        silence_merchant(db, "GITHUB")

    with session_scope() as db:
        tx = _simulate(db, "$21.00 at GITHUB.")
        assert tx.status == TransactionStatus.no_receipt_required
