from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count

# 萎凋槽状态：这三个码与下方迁移表是全应用唯一的规则定义。
_STATUS_LOADING = "loading"
_STATUS_WITHERING = "withering"
_STATUS_READY = "ready"
_STATUS_CHOICES = [
    (_STATUS_LOADING, "装叶中"),
    (_STATUS_WITHERING, "萎凋中"),
    (_STATUS_READY, "可下槽"),
]
_STATUS_LABELS = dict(_STATUS_CHOICES)
_VALID_STATUSES = frozenset(_STATUS_LABELS)

# 允许边：装叶中 → 萎凋中；萎凋中 → 可下槽；可下槽 → 装叶中。
# 判定逻辑只准写在 Trough.clean() 这一处，表单、视图、Admin、种子都不许另写。
_ALLOWED_TRANSITIONS = {
    _STATUS_LOADING: frozenset({_STATUS_WITHERING}),
    _STATUS_WITHERING: frozenset({_STATUS_READY}),
    _STATUS_READY: frozenset({_STATUS_LOADING}),
}

# 迁入「可下槽」时，最新批次实测含水率上限（%，含边界）。
_READY_MOISTURE_LIMIT = Decimal("40")


class Garden(models.Model):
    name = models.CharField("茶园名称", max_length=120)
    altitudeBand = models.CharField("海拔带", max_length=60)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "茶园"
        verbose_name_plural = "茶园"

    def __str__(self):
        return self.name


class Trough(models.Model):
    STATUS_LOADING = _STATUS_LOADING
    STATUS_WITHERING = _STATUS_WITHERING
    STATUS_READY = _STATUS_READY
    STATUS_CHOICES = _STATUS_CHOICES
    STATUS_LABELS = _STATUS_LABELS
    VALID_STATUSES = _VALID_STATUSES
    ALLOWED_TRANSITIONS = _ALLOWED_TRANSITIONS
    READY_MOISTURE_LIMIT = _READY_MOISTURE_LIMIT

    garden = models.ForeignKey(
        Garden,
        on_delete=models.CASCADE,
        related_name="troughs",
        verbose_name="茶园",
    )
    troughCode = models.CharField("槽位编号", max_length=40)
    cultivar = models.CharField("茶树品种", max_length=80)
    loadKg = models.DecimalField("装叶量(kg)", max_digits=10, decimal_places=2)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_LOADING,
    )

    class Meta:
        ordering = ["garden__name", "troughCode"]
        verbose_name = "萎凋槽"
        verbose_name_plural = "萎凋槽"
        constraints = [
            models.UniqueConstraint(
                fields=["garden", "troughCode"],
                name="uniq_trough_code_per_garden",
            ),
            models.CheckConstraint(
                name="trough_status_valid",
                check=models.Q(status__in=sorted(_VALID_STATUSES)),
            ),
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    # ---- 集中判定：以下三个 classmethod 是全应用唯一的规则出口 ----

    @classmethod
    def allowed_target_statuses(cls, current):
        """current 状态允许迁入的目标状态集合。

        current=None 表示新建入场，只许以「装叶中」入场；
        已存在的槽位额外允许保持原态（仅编辑其他字段）。
        """
        if current is None:
            return frozenset({cls.STATUS_LOADING})
        return cls.ALLOWED_TRANSITIONS.get(current, frozenset()) | {current}

    @classmethod
    def transition_edges_display(cls):
        """说明文案用的允许边，如「装叶中 → 萎凋中」，顺序固定。"""
        return [
            f"{cls.STATUS_LABELS[src]} → {cls.STATUS_LABELS[dst]}"
            for src, targets in cls.ALLOWED_TRANSITIONS.items()
            for dst in sorted(targets)
        ]

    @classmethod
    def status_counts(cls):
        """首页状态卡与槽列表状态过滤共用的唯一计数来源。

        一次 GROUP BY 查询返回 {状态码: 行数}，未出现的状态补 0。
        列表 ?status= 过滤与本计数基于同一张表同一状态列，改态后误差为 0。
        """
        grouped = dict(
            cls.objects.values("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        return {code: grouped.get(code, 0) for code in cls.STATUS_LABELS}

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    def _latest_batch_ready(self):
        """最新批次实测含水率是否已填且不超过 40%。"""
        if not self.pk:
            return False
        latest = self.latest_batch()
        return (
            latest is not None
            and latest.actualMoisture is not None
            and latest.actualMoisture <= self.READY_MOISTURE_LIMIT
        )

    def clean(self):
        """唯一的状态迁移判定点：非法迁移一律在此以中文拒绝。"""
        super().clean()
        errors = {}

        if not self.pk:
            if self.status != self.STATUS_LOADING:
                errors["status"] = (
                    f"新建萎凋槽只能以「{self.STATUS_LABELS[self.STATUS_LOADING]}」"
                    f"入场，不能直接建为"
                    f"「{self.STATUS_LABELS.get(self.status, self.status)}」。"
                )
        else:
            old_status = (
                Trough.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if old_status is not None and self.status != old_status:
                if self.status not in self.ALLOWED_TRANSITIONS.get(old_status, frozenset()):
                    targets = sorted(
                        self.ALLOWED_TRANSITIONS.get(old_status, frozenset())
                    )
                    allowed = "、".join(
                        f"「{self.STATUS_LABELS[code]}」" for code in targets
                    ) or "无"
                    errors["status"] = (
                        f"非法状态迁移：{self.STATUS_LABELS[old_status]} → "
                        f"{self.STATUS_LABELS.get(self.status, self.status)}，已拒绝。"
                        f"允许的状态迁移只有：{'、'.join(self.transition_edges_display())}"
                        f"；当前状态「{self.STATUS_LABELS[old_status]}」只许迁移到 "
                        f"{allowed}。"
                    )
                elif self.status == self.STATUS_READY and not self._latest_batch_ready():
                    errors["status"] = (
                        "无法设为「可下槽」：最新萎凋批次的实测含水率必须已填写"
                        "且不超过 40%。"
                    )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WitherBatch(models.Model):
    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="萎凋槽",
    )
    startedAt = models.DateTimeField("开始时间")
    targetMoisture = models.DecimalField(
        "目标含水率(%)", max_digits=5, decimal_places=2
    )
    actualMoisture = models.DecimalField(
        "实测含水率(%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    rollGrade = models.CharField("揉捻等级", max_length=40)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "萎凋批次"
        verbose_name_plural = "萎凋批次"

    def __str__(self):
        return f"{self.trough} @ {self.startedAt:%Y-%m-%d %H:%M}"
