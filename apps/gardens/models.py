from django.core.exceptions import ValidationError
from django.db import models


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
    STATUS_LOADING = "loading"
    STATUS_WITHERING = "withering"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_LOADING, "装叶中"),
        (STATUS_WITHERING, "萎凋中"),
        (STATUS_READY, "可下槽"),
    ]

    # 允许的状态迁移边（唯一权威定义，表单/模型校验/列表/统计全部由此派生）：
    #   装叶中 -> 萎凋中
    #   萎凋中 -> 可下槽（且最新批次实测含水率已填且 <= READY_MAX_MOISTURE）
    #   可下槽 -> 装叶中
    ALLOWED_TRANSITIONS = {
        STATUS_LOADING: (STATUS_WITHERING,),
        STATUS_WITHERING: (STATUS_READY,),
        STATUS_READY: (STATUS_LOADING,),
    }
    READY_MAX_MOISTURE = 40

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
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    @classmethod
    def status_label(cls, status):
        return dict(cls.STATUS_CHOICES).get(status, status)

    @classmethod
    def transition_error(cls, from_status, to_status, latest_batch=None):
        """改态判定的唯一入口。返回 None 表示允许，否则返回中文错误说明。"""
        if to_status == from_status:
            return None
        allowed = cls.ALLOWED_TRANSITIONS.get(from_status, ())
        if to_status not in allowed:
            allowed_text = "、".join(
                f"「{cls.status_label(s)}」" for s in allowed
            ) or "（无）"
            return (
                f"非法状态迁移：不能从「{cls.status_label(from_status)}」"
                f"直接变更为「{cls.status_label(to_status)}」。"
                f"允许的迁移目标：{allowed_text}。"
            )
        if to_status == cls.STATUS_READY:
            if (
                latest_batch is None
                or latest_batch.actualMoisture is None
                or latest_batch.actualMoisture > cls.READY_MAX_MOISTURE
            ):
                return (
                    "无法设为可下槽：最新萎凋批次的实测含水率必须已填写"
                    f"且不超过 {cls.READY_MAX_MOISTURE}%。"
                )
        return None

    def status_transition_error(self, to_status):
        """以数据库中的当前状态为起点，校验能否迁移到 to_status。"""
        if self.pk:
            from_status = (
                Trough.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
                or self.STATUS_LOADING
            )
        else:
            # 新槽位尚未入库，视为从初始状态「装叶中」出发
            from_status = self.STATUS_LOADING
        latest = None
        if self.pk:
            latest = (
                WitherBatch.objects.filter(trough_id=self.pk)
                .order_by("-startedAt", "-id")
                .first()
            )
        return self.transition_error(from_status, to_status, latest)

    @classmethod
    def status_summary(cls):
        """各状态槽位数：首页状态卡与槽列表状态过滤共用同一数据源。"""
        counts = {status: 0 for status, _ in cls.STATUS_CHOICES}
        for row in cls.objects.values("status").annotate(n=models.Count("id")):
            counts[row["status"]] = row["n"]
        return counts

    def clean(self):
        super().clean()
        error = self.status_transition_error(self.status)
        if error:
            raise ValidationError({"status": error})

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
