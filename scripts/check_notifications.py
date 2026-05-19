"""Check why TAKE_PROFIT / EXPIRE alerts were never seen by user."""
from database.connection import SQLServerConnection


def main():
    db = SQLServerConnection()

    print("=" * 80)
    print("ACTIVE TRADES daily_status vs notifications received")
    print("=" * 80)

    active = db.fetch_all(
        "SELECT trade_id, daily_status, exit_instruction FROM options_trades "
        "WHERE status='ACTIVE' ORDER BY executed_on"
    )
    for t in active:
        print(f"\n {t['trade_id']}  daily_status={t['daily_status']}")
        notifs = db.fetch_all(
            "SELECT created_at, notif_type, severity, title, read_at "
            "FROM options_notifications WHERE related_trade_id=? "
            "ORDER BY created_at DESC",
            [t["trade_id"]],
        )
        if not notifs:
            print(f"    -> NO NOTIFICATIONS in DB for this trade")
            continue
        for n in notifs:
            seen = "READ" if n["read_at"] else "UNREAD"
            print(
                f"    [{n['created_at']}] {n['notif_type']:<20} "
                f"({n['severity']}) {seen}: {n['title'][:80]}"
            )

    print("\n" + "=" * 80)
    print("ALL recent notifications in the system (last 20)")
    print("=" * 80)
    rows = db.fetch_all(
        "SELECT TOP 20 created_at, notif_type, severity, title, "
        "related_trade_id, read_at "
        "FROM options_notifications ORDER BY created_at DESC"
    )
    for n in rows:
        seen = "R" if n["read_at"] else "U"
        print(
            f"  [{n['created_at']}] [{seen}] {n['notif_type']:<20} "
            f"trade={n['related_trade_id'] or '-':<20} {n['title'][:60]}"
        )

    print("\n" + "=" * 80)
    print("Distinct notification types ever fired")
    print("=" * 80)
    rows = db.fetch_all(
        "SELECT notif_type, COUNT(*) AS n, MAX(created_at) AS last_seen "
        "FROM options_notifications GROUP BY notif_type ORDER BY n DESC"
    )
    for r in rows:
        print(f"  {r['notif_type']:<25} n={r['n']:>4}  last={r['last_seen']}")

    db.close()


if __name__ == "__main__":
    main()
