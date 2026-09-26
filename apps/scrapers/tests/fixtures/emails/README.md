# Synthetic notification emails

Every email in this directory is fabricated for the email-backend tests
(`tests/test_email_parser.py`). None is a real or redacted notification: the
senders are the institutions' notification domains, and every recipient,
counterparty, merchant, amount and Message-ID is invented, using the repo's
canonical synthetic figures (`$ 2.500.000`, `$ 1.000.000`, `$ 999.999`).

The wording approximates the institutions' notifications; when a real email
parses wrongly, add a fixture here that keeps its structure and wording but
fabricates every identifier and figure (see the personal-data policy).
