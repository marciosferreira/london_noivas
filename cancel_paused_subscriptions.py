import os
import stripe
import datetime

# 🔑 Configure sua chave secreta do Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def cancel_paused_subscriptions():
    now = datetime.datetime.now()
    cancelados = []

    subscriptions = stripe.Subscription.list(status="paused", limit=100)
    for sub in subscriptions.auto_paging_iter():
        sub_id = sub.id
        paused_since = datetime.datetime.fromtimestamp(sub.current_period_end)
        days_since_paused = (now - paused_since).days

        if days_since_paused > 7:
            stripe.Subscription.delete(sub_id)
            cancelados.append((sub_id, days_since_paused))
            print(f"✔️ Cancelada {sub_id} pausada há {days_since_paused} dias")

    print(f"Total canceladas: {len(cancelados)}")


if __name__ == "__main__":
    cancel_paused_subscriptions()
