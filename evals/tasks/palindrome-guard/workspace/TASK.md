# Task: palindrome-guard

Implement `is_palindrome(text)` in `palindrome.py`. The guard is
*normalizing*: fold case, drop every character that is not a letter or
a digit, then compare the residue with its reverse. So
`"A man, a plan, a canal: Panama"` is a palindrome, `"No 'x' in
Nixon"` is one, `"racecar"` is one, `"python"` is not, and text that
normalizes to nothing (empty, punctuation-only) counts as one. The
attached suite under `tests/` is the contract.
