# Task: fizzbuzz-off-by-one

`fizzbuzz.py` has one bug: `fizzbuzz(n)` walks `0..n-1` instead of
`1..n`. The classic sequence starts at 1 and includes `n`, and it never
mentions 0 (`0` is divisible by everything, so the bug turns the first
line into `"FizzBuzz"`). Fix the bounds; the attached suite under
`tests/` is the contract.
