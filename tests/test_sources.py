

def test_forwarded_newsletter_credits_the_original_sender():
    from market_updater.ingest.email_inbox import forwarded_origin
    text = ("________________________________\nFrom: Unhedged <unhedged@newsletters.ft.com>\nSent: 24 November 2026\n"
            "To: Saravanan,T\nSubject: The Jensen put\n\nBody of the letter...")
    assert forwarded_origin("Saravanan,T", "FW: The Jensen put", text) == ("Unhedged", "The Jensen put")
    assert forwarded_origin("Doomberg", "Energy notes", text) == ("Doomberg", "Energy notes")
