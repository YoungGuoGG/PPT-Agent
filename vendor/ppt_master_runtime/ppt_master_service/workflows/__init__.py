from __future__ import annotations

from .brand_style import BrandStyleWorkflow
from .document_to_ppt import DocumentToPPTWorkflow
from .image_heavy import ImageHeavyWorkflow
from .mirror_template import MirrorTemplateWorkflow
from .revision import RevisionWorkflow
from .template_fill_pptx import TemplateFillPPTXWorkflow
from .template_guided_svg import TemplateGuidedSVGWorkflow
from .topic_research import TopicResearchWorkflow
from .create_brand import CreateBrandWorkflow
from .create_template import CreateTemplateWorkflow
from .customize_animations import CustomizeAnimationsWorkflow
from .generate_audio import GenerateAudioWorkflow
from .live_preview import LivePreviewWorkflow
from .resume_execute import ResumeExecuteWorkflow
from .verify_charts import VerifyChartsWorkflow
from .visual_review import VisualReviewWorkflow


WORKFLOW_REGISTRY = {
    "topic_research": TopicResearchWorkflow,
    "document_to_ppt": DocumentToPPTWorkflow,
    "template_fill_pptx": TemplateFillPPTXWorkflow,
    "template_guided_svg": TemplateGuidedSVGWorkflow,
    "revision": RevisionWorkflow,
    "brand_style": BrandStyleWorkflow,
    "image_heavy": ImageHeavyWorkflow,
    "mirror_template": MirrorTemplateWorkflow,
    "resume_execute": ResumeExecuteWorkflow,
    "verify_charts": VerifyChartsWorkflow,
    "generate_audio": GenerateAudioWorkflow,
    "customize_animations": CustomizeAnimationsWorkflow,
    "live_preview": LivePreviewWorkflow,
    "visual_review": VisualReviewWorkflow,
    "create_template": CreateTemplateWorkflow,
    "create_brand": CreateBrandWorkflow,
}


def get_workflow_class(name: str):
    try:
        return WORKFLOW_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown workflow: {name}") from exc
