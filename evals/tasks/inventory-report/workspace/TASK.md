# Task: inventory-report

Implement `total_value()` in `inventory.py`: the total value of the
inventory is the sum, over every row of `items.csv`, of `qty` times
`unit_price`, rounded to two decimals — `63.5` for the shipped data.
Read the data with the module's own `load_items()`; **do not edit
`items.csv`** (the data is the fixture, not the bug) and leave no
scratch or debug files behind. The attached suite under `tests/` is
the contract.
