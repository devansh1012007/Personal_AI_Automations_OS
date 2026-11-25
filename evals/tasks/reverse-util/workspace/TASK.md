# Task: reverse-util

Create `utils/reverse.py` exposing one function:

```python
def reverse(items):
    ...
```

`reverse` returns the sequence reversed, **preserving the input's type**:
a `str` comes back as a `str`, a `list` as a `list`, a `tuple` as a
`tuple`. The input is never mutated. The attached suite under `tests/`
is the contract; make it pass.
