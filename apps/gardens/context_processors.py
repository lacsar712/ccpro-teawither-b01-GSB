"""模板全局上下文：允许边等说明一律从模型唯一规则派生。"""
from .models import Trough


def trough_rules(request):
    return {
        "trough_transition_edges": Trough.transition_edges_display(),
        "trough_ready_moisture_limit": Trough.READY_MOISTURE_LIMIT,
    }
