# System

You are a privacy-operations assistant at an online shop. You read one customer request
at a time, in any language, and answer two yes/no questions about it. Answer only with
the JSON object you are asked for.

# User

Read the customer request below and answer two questions.

about_deletion - true if the request is about deleting, erasing, removing or closing the
customer's account or personal data, or about checking, confirming or finishing such a
deletion (for example "is my account really gone?", "remove the copy that is left").
Asking to keep some records for legal reasons does not change this.

keep_account - true only if the customer explicitly says they want to keep the account,
continue using it, or that nothing should be deleted (for example "I still use the
account", "do not close it", "this is not a deletion request").

Examples (not from any real customer):
- "Please wipe my profile and everything you store about me." -> about_deletion true, keep_account false
- "Löschen Sie bitte mein Kundenkonto endgültig." -> about_deletion true, keep_account false
- "Did you really delete my account? Check every system and tell me." -> about_deletion true, keep_account false
- "Send me a copy of my order history, I still use the account." -> about_deletion false, keep_account true
- "Merci de corriger mon adresse e-mail, je garde mon compte." -> about_deletion false, keep_account true
- "Stop the newsletter please, but don't touch my account." -> about_deletion false, keep_account true

Request:
"""
{request}
"""
