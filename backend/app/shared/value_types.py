"""Nominal, dependency-free value types shared across source and ontology ports."""


class JsonText(str):
    """One non-SQL-NULL JSON cell, retaining the driver's exact JSON text.

    SQL NULL remains None. In particular, JsonText('null') is a present JSON
    value, and arbitrary JSON numbers must never round-trip through float or
    JavaScript's number type. The wire encoding is advertised as json_text.
    """
