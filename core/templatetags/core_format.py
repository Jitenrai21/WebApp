from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


def _group_indian_digits(integer_part: str) -> str:
    if len(integer_part) <= 3:
        return integer_part

    last_three = integer_part[-3:]
    leading = integer_part[:-3]
    groups = []

    while len(leading) > 2:
        groups.insert(0, leading[-2:])
        leading = leading[:-2]

    if leading:
        groups.insert(0, leading)

    return ",".join(groups + [last_three])


@register.filter
def npr_amount(value):
    """Format numeric values in NPR style with US-grouping commas.

    Examples:
    - 1000 -> 1,000
    - 1250.5 -> 1,250.50
    - 1250.0 -> 1,250
    """
    if value in (None, ""):
        return "0"

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return value

    amount = amount.quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)

    fixed = f"{absolute:.2f}"
    integer_part, fractional_part = fixed.split(".")
    grouped_integer = _group_indian_digits(integer_part)

    if fractional_part == "00":
        return f"{sign}{grouped_integer}"

    return f"{sign}{grouped_integer}.{fractional_part}"
