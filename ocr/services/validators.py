import re


def validate_cell_value(field_type, value):
    value = (value or "").strip()
    if field_type == "N":
        if value and not re.fullmatch(r"\d{2,4}", value):
            raise ValueError("N fields must be 2 to 4 digits.")
    elif field_type == "F":
        if value and not value.isdigit():
            raise ValueError("F fields must contain digits only.")
    elif field_type == "S":
        if value and not (value.lower() == "x" or value.isdigit()):
            raise ValueError('S fields must be empty, "x", or digits.')
    else:
        raise ValueError("Unknown field type.")
    return value
