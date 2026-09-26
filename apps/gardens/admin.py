from django.contrib import admin

from .forms import TroughForm
from .models import Garden, Trough, WitherBatch


@admin.register(Garden)
class GardenAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "altitudeBand")
    search_fields = ("name", "altitudeBand")


@admin.register(Trough)
class TroughAdmin(admin.ModelAdmin):
    # Admin 复用同一表单（下拉收窄）；保存仍归模型唯一的 clean() 判定。
    form = TroughForm
    list_display = ("id", "garden", "troughCode", "cultivar", "loadKg", "status")
    list_filter = ("status", "garden")
    search_fields = ("troughCode", "cultivar")


@admin.register(WitherBatch)
class WitherBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "trough",
        "startedAt",
        "targetMoisture",
        "actualMoisture",
        "rollGrade",
    )
    list_filter = ("rollGrade",)
