from .constants import NUMBER_WORDS


def _envar_name(link_name: str, prop_name: str) -> str:
    cleaned_link_name = "".join(c if c.isalnum() else "_" for c in link_name)

    if (first_char := cleaned_link_name[0]) and first_char.isdigit():
        cleaned_link_name = NUMBER_WORDS[first_char] + cleaned_link_name[1:]

    return f"STLV_{cleaned_link_name.upper()}_{prop_name.upper()}"
