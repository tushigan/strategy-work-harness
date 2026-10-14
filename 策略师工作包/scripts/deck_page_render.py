"""Escaped offline page-plan rendering; never accepts raw HTML or remote media."""
import base64
from html import escape


def render(page, assets):
    number, kind = page["page"], page["kind"]
    index = 0

    def field(value, tag="span", attrs=""):
        nonlocal index
        index += 1
        return f'<{tag} data-edit="{number}-body-{index}"{attrs}>{escape(str(value))}</{tag}>'

    content = '<ul class="page-points">' + ''.join(field(v, "li") for v in page["bullets"]) + '</ul>'
    if kind == "table":
        table = page["table"]
        content += '<table class="comparison"><thead><tr>' + ''.join(
            field(v, "th", ' scope="col"') for v in table["columns"]) + '</tr></thead><tbody>'
        content += ''.join('<tr>' + ''.join(field(v, "td") for v in row) + '</tr>' for row in table["rows"])
        content += '</tbody></table>'
    elif kind == "chart":
        chart = page["chart"]
        maximum = max((v["value"] for v in chart["series"]), default=0) or 1
        content += '<figure class="bar-chart">' + field(chart["unit"], "figcaption")
        for item in chart["series"]:
            width = 100 * item["value"] / maximum
            content += '<div class="bar-row">' + field(item["label"], "span", ' class="bar-label"')
            content += f'<div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:{width:.6f}%"></div></div>'
            content += field(item["value"], "span", ' class="bar-value"') + '</div>'
        content += '</figure>'
    elif kind == "card":
        card = page["card"]
        snapshot = f' data-pingtai-snapshot="{escape(card["snapshot"], quote=True)}"' if card.get("snapshot") else ""
        content += (f'<div class="product-card" data-pingtai-sku="{escape(card["sku"], quote=True)}"{snapshot}>'
                    + field(card["role"], "p", ' class="card-role"') + field(card["positioning"], "p", ' class="card-positioning"')
                    + '<dl class="card-facts">')
        for fact in card["facts"]:
            content += ('<div class="card-fact">' + field(fact["label"], "dt")
                        + field(fact["value"], "dd", f' data-field="{escape(fact["field"], quote=True)}"') + '</div>')
        content += '</dl>' + field(card["channel"], "p", ' class="card-channel"') + field(card["strategy"], "p", ' class="card-strategy"') + '</div>'
    elif kind == "pyramid":
        content += '<div class="pyramid" role="list">'
        for number_in, layer in enumerate(page["pyramid"]["layers"], 1):
            content += f'<div class="pyramid-layer layer-{number_in}" role="listitem">' + field(layer["name"], "strong")
            if "role" in layer:
                content += field(layer["role"], "em", ' class="layer-role"')
            content += ''.join(field(v, "span", ' class="layer-item"') for v in layer["items"]) + '</div>'
        content += '</div>'
    elif kind == "image":
        picture = page["image"]
        _, raw, mime = assets['asset-' + picture["sha256"]]
        encoded = base64.b64encode(raw).decode("ascii")
        content += (f'<figure class="page-image"><img src="data:{mime};base64,{encoded}" '
                    f'alt="{escape(picture["alt"], quote=True)}">' + field(picture["caption"], "figcaption") + '</figure>')
    return content
