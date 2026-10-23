def normalize(text):
    return " ".join(text.lower().split())


def slugify(text):
    return normalize(text).replace(" ", "-")
