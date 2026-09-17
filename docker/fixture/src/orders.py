"""Order maths. Referenced from the acceptance assertions by name."""


def apply_discount(total, rate):
    return total - (total * rate)


def order_total(items, rate):
    total = sum(i["price"] for i in items)
    return apply_discount(total, rate)
