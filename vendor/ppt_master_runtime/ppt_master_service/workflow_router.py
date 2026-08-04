from __future__ import annotations

from .schemas import GeneratePPTRequest


# Chinese triggers are written as Unicode escapes to keep this file ASCII-safe on
# Windows shells with non-UTF-8 default encodings.
REVISION_KEYWORDS = (
    "\u4fee\u6539",  # modify
    "\u7f8e\u5316",  # beautify
    "\u6da6\u8272",  # polish
    "\u7ffb\u8bd1",  # translate
    "\u538b\u7f29",  # compress
    "\u6539\u5199",  # rewrite
    "\u8c03\u6574",  # adjust
    "\u4f18\u5316",  # optimize
    "\u6539\u98ce\u683c",  # restyle
    "\u6539\u6392\u7248",  # relayout
    "\u5df2\u6709ppt",  # existing ppt
    "\u5df2\u6709 ppt",  # existing ppt
    "revise",
    "revision",
    "beautify",
    "polish",
    "translate",
    "compress",
    "shorten",
    "restyle",
    "relayout",
    "existing ppt",
    "existing deck",
)

IMAGE_HEAVY_KEYWORDS = (
    "\u5f3a\u56fe\u7247",  # image-heavy
    "\u56fe\u7247\u98ce\u683c",  # image style
    "\u591a\u56fe",  # many images
    "\u89c6\u89c9\u5927\u7247",  # visual blockbuster
    "\u63d2\u753b",  # illustration
    "\u7ed8\u672c",  # picture book
    "\u6d77\u62a5\u611f",  # poster-like
    "\u5927\u91cf\u56fe\u7247",  # lots of images
    "\u89c6\u89c9\u51b2\u51fb",  # visual impact
    "\u4ea7\u54c1\u56fe",  # product images
    "\u5efa\u7b51\u5c55\u793a",  # architecture showcase
    "\u4ea7\u54c1\u53d1\u5e03",  # product launch
    "\u513f\u7ae5\u7ed8\u672c",  # children's picture book
    "image heavy",
    "image-heavy",
    "visual-heavy",
    "photo driven",
    "photo-driven",
    "illustration",
    "storybook",
    "picture book",
    "poster",
    "visual impact",
    "product image",
    "product launch",
    "architecture showcase",
)

MIRROR_TEMPLATE_KEYWORDS = (
    "mirror template",
    "mirror",
    "\u955c\u50cf\u6a21\u677f",  # mirror template
    "\u590d\u523b\u6a21\u677f",  # replicate template
    "\u4e00\u6bd4\u4e00",  # one-to-one
    "1:1",
)

BRAND_STYLE_KEYWORDS = (
    "\u54c1\u724c",  # brand
    "\u516c\u53f8\u98ce\u683c",  # company style
    "\u516c\u53f8vi",  # company VI
    "\u54c1\u724c\u8272",  # brand color
    "\u54c1\u724c\u624b\u518c",  # brand guide
    "logo",
    "brand",
    "brand style",
    "brand color",
    "brand guide",
    "company style",
)

TEMPLATE_FILL_KEYWORDS = (
    "\u4fdd\u7559\u539f\u7248\u5f0f",  # keep original layout
    "\u4fdd\u7559\u539f\u6a21\u677f",  # keep original template
    "\u539f\u7248\u5f0f",  # original layout
    "\u586b\u5145\u6a21\u677f",  # fill template
    "\u5957\u6a21\u677f",  # fit into template
    "\u4f7f\u7528\u516c\u53f8\u6a21\u677f",  # use company template
    "\u516c\u53f8\u6a21\u677f",  # company template
    "\u4fdd\u6301\u7248\u5f0f",  # preserve layout
    "\u66ff\u6362\u5185\u5bb9",  # replace content
    "fill template",
    "template fill",
    "fill this deck",
    "use company template",
    "company template",
    "preserve layout",
    "keep layout",
    "keep original layout",
    "reuse this deck",
    "replace copy",
)

TEMPLATE_GUIDED_KEYWORDS = (
    "\u81ea\u7531\u8bbe\u8ba1",  # freely design
    "\u81ea\u7531\u6392\u7248",  # free layout
    "\u4e0d\u53d7\u539f\u6587\u672c\u6846\u9650\u5236",  # not limited by original text boxes
    "\u53c2\u8003\u6a21\u677f\u98ce\u683c",  # use template style as reference
    "\u6839\u636e\u6a21\u677f\u98ce\u683c",  # design from template style
    "template-guided",
    "freely design",
    "free layout",
    "use template style",
)

IMAGE_SOURCE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg")


def route_workflow(request: GeneratePPTRequest) -> str:
    if request.mode != "auto":
        return request.mode

    text = " ".join(
        part
        for part in (
            request.topic,
            request.prompt,
            request.style,
            request.extra_requirements,
        )
        if part
    ).lower()

    source_has_pptx = any(
        item.lower().endswith((".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"))
        for item in (request.source_files or [])
    )
    source_has_image = any(
        item.lower().endswith(IMAGE_SOURCE_SUFFIXES)
        for item in (request.source_files or [])
    )

    if request.mirror_template_id or any(keyword in text for keyword in MIRROR_TEMPLATE_KEYWORDS):
        return "mirror_template"
    if request.reference_pptx and any(keyword in text for keyword in REVISION_KEYWORDS):
        return "revision"
    if source_has_pptx and any(keyword in text for keyword in REVISION_KEYWORDS):
        return "revision"
    if request.template_pptx and (
        request.template_strategy == "guided_svg"
        or any(keyword in text for keyword in TEMPLATE_GUIDED_KEYWORDS)
    ):
        return "template_guided_svg"
    if request.template_pptx:
        return "template_fill_pptx"
    if any(keyword in text for keyword in TEMPLATE_FILL_KEYWORDS):
        return "template_fill_pptx"
    if request.brand_assets or any(keyword in text for keyword in BRAND_STYLE_KEYWORDS):
        return "brand_style"
    if source_has_image or any(keyword in text for keyword in IMAGE_HEAVY_KEYWORDS):
        return "image_heavy"
    if request.source_files or request.source_urls:
        return "document_to_ppt"
    if request.topic or request.prompt:
        return "topic_research"
    return "document_to_ppt"
