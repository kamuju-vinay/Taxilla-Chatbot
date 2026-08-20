"""
Runs both fetchers on a fixed interval (near-real-time polling, works
everywhere with no extra setup — configurable down to 30 seconds). Also
manages renewal of the two real-time push mechanisms (Gmail Pub/Sub watch,
Outlook Graph subscription) when PUBLIC_BASE_URL is configured, since both
expire and need periodic renewal to keep working.
"""
import logging

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scheduler")


def run_sync_once():
    """Runs whichever providers are enabled. Never raises — collects errors instead,
    so one provider being unconfigured doesn't block the other."""
    results = []

    if config.GMAIL_ENABLED:
        try:
            if config.GMAIL_AUTH_METHOD == "imap":
                from fetchers import gmail_imap_fetcher as gmail_fetcher
            else:
                from fetchers import gmail_fetcher
            results.append(gmail_fetcher.fetch_new_reports())
        except Exception as e:
            log.exception("Gmail sync failed")
            results.append({"provider": "gmail", "error": str(e)})

    if config.OUTLOOK_ENABLED:
        try:
            from fetchers import outlook_fetcher
            results.append(outlook_fetcher.fetch_new_reports())
        except Exception as e:
            log.exception("Outlook sync failed")
            results.append({"provider": "outlook", "error": str(e)})

    return results


def _renew_push_subscriptions():
    """Best-effort renewal of both push mechanisms. Silently does nothing
    for a provider that isn't enabled, has no active subscription/watch
    yet, or isn't using OAuth (Gmail IMAP mode has no push equivalent)."""
    if config.GMAIL_ENABLED and config.GMAIL_AUTH_METHOD == "oauth" and config.GMAIL_PUBSUB_TOPIC:
        try:
            from fetchers import gmail_fetcher
            if gmail_fetcher.watch_status():
                gmail_fetcher.start_watch()  # watch() calls are idempotent — safe to just re-call
                log.info("Renewed Gmail push watch")
        except Exception:
            log.exception("Gmail push watch renewal failed")

    if config.OUTLOOK_ENABLED and config.PUBLIC_BASE_URL:
        try:
            from fetchers import outlook_fetcher
            if outlook_fetcher.subscription_status():
                outlook_fetcher.renew_subscription()
                log.info("Renewed Outlook push subscription")
        except Exception:
            log.exception("Outlook push subscription renewal failed")


def start_background_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_sync_once,
        "interval",
        seconds=config.POLL_INTERVAL_SECONDS,
        next_run_time=None,  # first run kicked off explicitly in app.py after startup
        id="mailbox_sync",
    )
    # Push subscriptions/watches expire well before a day passes (Gmail:
    # ~7 days, Outlook: ~3 days) — checking hourly is cheap and comfortably
    # inside both windows.
    scheduler.add_job(
        _renew_push_subscriptions,
        "interval",
        hours=1,
        id="push_renewal",
    )
    scheduler.start()
    log.info("Scheduler started — polling every %s second(s)", config.POLL_INTERVAL_SECONDS)
    return scheduler


def reschedule_polling(scheduler):
    """Call after changing POLL_INTERVAL_SECONDS at runtime so the change
    takes effect without restarting the process."""
    scheduler.reschedule_job("mailbox_sync", trigger="interval", seconds=config.POLL_INTERVAL_SECONDS)
    log.info("Rescheduled polling to every %s second(s)", config.POLL_INTERVAL_SECONDS)
