"""Shared contract every receipt provider implements.

The schema and instructions live here so swapping providers can never change
what the frontend receives.
"""

import logging

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider failed. `transient` marks it worth retrying or failing over."""

    def __init__(self, message: str, *, transient: bool = False, status: int = 502):
        super().__init__(message)
        self.message = message
        self.transient = transient
        self.status = status


# Categories matter to the split, not just the display. Service charge and tax
# have to be re-apportioned across people in proportion to what each person
# actually ate — splitting them evenly is what made the old bills unfair — so
# the model has to tell us which lines are charges rather than food.
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "currency": {
            "type": "string",
            "description": "ISO 4217 code if determinable, else empty string.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {
                        "type": "integer",
                        "description": "Units on this line. 1 when not printed.",
                    },
                    "line_total": {
                        "type": "number",
                        "description": "Total for the whole line, not the unit price.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "item",
                            "service_charge",
                            "tax",
                            "discount",
                            "rounding",
                        ],
                    },
                },
                "required": ["name", "quantity", "line_total", "category"],
            },
        },
        "subtotal": {"type": "number"},
        "total": {
            "type": "number",
            "description": "Grand total as printed. 0 if not shown.",
        },
    },
    "required": ["currency", "items", "subtotal", "total"],
}

SYSTEM_INSTRUCTION = """\
You read restaurant receipts and return structured data. Rules:

- Return every charged line, in the order printed.
- `line_total` is the amount printed for that line — the total for all units on
  it, never the unit price. If only a unit price is printed, multiply it out.
- `quantity` is the number of units on the line; use 1 when none is printed.
- Classify each line: food and drink are "item"; service charge is
  "service_charge"; GST/VAT/sales tax is "tax"; discounts and vouchers are
  "discount" (negative line_total); rounding adjustments are "rounding".
- Sub-items printed under a set or combo with no price of their own are not
  separate lines. Fold their names into the parent, e.g.
  "Cocktail Party for 2 (Pineapple Rum, Ume Dream)".
- Receipts may be in any language. Keep item names in the language printed.
- If a price is smudged or unreadable, use 0 rather than guessing.
- `subtotal` is the pre-charge total; `total` is the grand total as printed.
"""

PROMPT = "Extract every line from this receipt."
