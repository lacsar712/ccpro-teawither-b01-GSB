"""萎凋槽状态迁移规则测试。

核心约束：
- 允许边只有 装叶中→萎凋中、萎凋中→可下槽、可下槽→装叶中；
- 非法迁移（含装叶中→可下槽、可下槽→萎凋中）一律中文拒绝；
- 迁入可下槽须最新批次实测含水率有值且 ≤40%；
- 判定只在 Trough.clean() 一处，表单伪造 POST 同样被拦；
- 首页状态卡与列表 ?status= 过滤行数同源，改态前后误差为 0；
- 种子覆盖三种状态。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import TroughForm
from .models import Garden, Trough, WitherBatch

ALL_STATUSES = [
    Trough.STATUS_LOADING,
    Trough.STATUS_WITHERING,
    Trough.STATUS_READY,
]
LABELS = Trough.STATUS_LABELS


def make_trough(code="T-1", status=Trough.STATUS_LOADING):
    """不经模型校验地造出某状态的槽（仅用于准备测试前置态）。"""
    garden, _ = Garden.objects.get_or_create(
        name="测试园", defaults={"altitudeBand": "500m"}
    )
    t = Trough.objects.create(
        garden=garden, troughCode=code, cultivar="品种",
        loadKg=Decimal("10.00"),
    )
    if status != Trough.STATUS_LOADING:
        Trough.objects.filter(pk=t.pk).update(status=status)
        t.refresh_from_db()
    return t


def add_batch(trough, moisture, hours_ago=1):
    return WitherBatch.objects.create(
        trough=trough,
        startedAt=timezone.now() - timezone.timedelta(hours=hours_ago),
        targetMoisture=Decimal("40.00"),
        actualMoisture=(None if moisture is None else Decimal(str(moisture))),
        rollGrade="级",
    )


def transition(trough, new_status):
    trough.status = new_status
    trough.save()


class AllowedEdgesTests(TestCase):
    def test_only_three_edges_allowed(self):
        # 装叶中 → 萎凋中
        t = make_trough("A-1")
        transition(t, Trough.STATUS_WITHERING)
        self.assertEqual(t.status, Trough.STATUS_WITHERING)

        # 萎凋中 → 可下槽（最新实测 40.00%，边界值允许）
        add_batch(t, "40.00")
        transition(t, Trough.STATUS_READY)
        self.assertEqual(t.status, Trough.STATUS_READY)

        # 可下槽 → 装叶中
        transition(t, Trough.STATUS_LOADING)
        self.assertEqual(t.status, Trough.STATUS_LOADING)

    def test_every_other_pair_is_rejected_with_chinese_message(self):
        legal = {
            (Trough.STATUS_LOADING, Trough.STATUS_WITHERING),
            (Trough.STATUS_WITHERING, Trough.STATUS_READY),
            (Trough.STATUS_READY, Trough.STATUS_LOADING),
        }
        for old in ALL_STATUSES:
            for new in ALL_STATUSES:
                if old == new:
                    continue  # 保持原态不算迁移，另由 UnchangedStatusTests 覆盖
                if (old, new) in legal:
                    continue
                with self.subTest(old=old, new=new):
                    t = make_trough(f"X-{old}-{new}", status=old)
                    add_batch(t, "30.00")  # 含水率门槛全部满足，只验迁移边
                    with self.assertRaises(ValidationError) as ctx:
                        transition(t, new)
                    self.assertEqual(Trough.objects.get(pk=t.pk).status, old)
                    msg = str(ctx.exception.message_dict["status"][0])
                    self.assertIn("非法状态迁移", msg)
                    self.assertIn("已拒绝", msg)
                    self.assertIn(LABELS[old], msg)
                    self.assertIn(LABELS[new], msg)
                    self.assertIn("装叶中 → 萎凋中", msg)
                    self.assertIn("萎凋中 → 可下槽", msg)
                    self.assertIn("可下槽 → 装叶中", msg)

    def test_loading_cannot_jump_directly_to_ready(self):
        t = make_trough("J-1")
        add_batch(t, "30.00")
        with self.assertRaises(ValidationError):
            transition(t, Trough.STATUS_READY)
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_LOADING)

    def test_ready_cannot_go_back_to_withering(self):
        t = make_trough("J-2", status=Trough.STATUS_WITHERING)
        add_batch(t, "30.00")
        transition(t, Trough.STATUS_READY)
        with self.assertRaises(ValidationError):
            transition(t, Trough.STATUS_WITHERING)
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_READY)


class ReadyMoistureGateTests(TestCase):
    def test_missing_moisture_blocks_ready(self):
        t = make_trough("M-1", status=Trough.STATUS_WITHERING)
        add_batch(t, None)
        with self.assertRaises(ValidationError) as ctx:
            transition(t, Trough.STATUS_READY)
        self.assertIn("实测含水率", str(ctx.exception.message_dict["status"][0]))

    def test_moisture_over_40_blocks_ready(self):
        t = make_trough("M-2", status=Trough.STATUS_WITHERING)
        add_batch(t, "40.01")
        with self.assertRaises(ValidationError):
            transition(t, Trough.STATUS_READY)

    def test_moisture_exactly_40_allows_ready(self):
        t = make_trough("M-3", status=Trough.STATUS_WITHERING)
        add_batch(t, "40.00")
        transition(t, Trough.STATUS_READY)
        self.assertEqual(t.status, Trough.STATUS_READY)

    def test_no_batch_at_all_blocks_ready(self):
        t = make_trough("M-4", status=Trough.STATUS_WITHERING)
        with self.assertRaises(ValidationError):
            transition(t, Trough.STATUS_READY)

    def test_latest_batch_is_what_counts(self):
        # 最新批次超 40%，即使更早的批次合格也不许进可下槽。
        t = make_trough("M-5", status=Trough.STATUS_WITHERING)
        add_batch(t, "30.00", hours_ago=5)
        add_batch(t, "45.00", hours_ago=1)
        with self.assertRaises(ValidationError):
            transition(t, Trough.STATUS_READY)

        # 补一条合格的最新批次后即可通过。
        add_batch(t, "38.00", hours_ago=0)
        transition(t, Trough.STATUS_READY)
        self.assertEqual(t.status, Trough.STATUS_READY)

    def test_other_edges_do_not_require_moisture(self):
        # 装叶中→萎凋中、可下槽→装叶中不看含水率。
        t = make_trough("M-6")
        transition(t, Trough.STATUS_WITHERING)
        add_batch(t, None)
        # 可下槽→装叶中：先合法地造一个 ready 前置态
        t2 = make_trough("M-7", status=Trough.STATUS_WITHERING)
        add_batch(t2, "35.00")
        transition(t2, Trough.STATUS_READY)
        WitherBatch.objects.filter(trough=t2).update(actualMoisture=None)
        transition(t2, Trough.STATUS_LOADING)
        self.assertEqual(t2.status, Trough.STATUS_LOADING)


class CreateEntryTests(TestCase):
    def test_new_trough_defaults_to_loading(self):
        t = make_trough("C-1")
        self.assertEqual(t.status, Trough.STATUS_LOADING)

    def test_new_trough_cannot_start_withering_or_ready(self):
        garden = Garden.objects.create(name="入园园", altitudeBand="600m")
        for code, bad in [("C-2", Trough.STATUS_WITHERING), ("C-3", Trough.STATUS_READY)]:
            with self.subTest(bad=bad):
                t = Trough(
                    garden=garden, troughCode=code, cultivar="x",
                    loadKg=Decimal("1.00"), status=bad,
                )
                with self.assertRaises(ValidationError) as ctx:
                    t.save()
                self.assertIn("入场", str(ctx.exception.message_dict["status"][0]))


class UnchangedStatusTests(TestCase):
    def test_editing_other_fields_keeps_any_status(self):
        for status in ALL_STATUSES:
            with self.subTest(status=status):
                t = make_trough(f"U-{status}", status=status)
                add_batch(t, "30.00")
                t.cultivar = "改名品种"
                t.save()  # 状态未变，任何状态下都应允许
                t.refresh_from_db()
                self.assertEqual(t.cultivar, "改名品种")
                self.assertEqual(t.status, status)


class FormTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="pw")
        self.garden = Garden.objects.create(name="表单园", altitudeBand="700m")

    def test_widget_only_offers_allowed_targets(self):
        t = make_trough("F-1")
        form = TroughForm(instance=t)
        offered = {c for c, _ in form.fields["status"].widget.choices}
        self.assertEqual(
            offered,
            {Trough.STATUS_LOADING, Trough.STATUS_WITHERING},
        )

    def test_create_form_widget_only_offers_loading(self):
        form = TroughForm()
        offered = {c for c, _ in form.fields["status"].widget.choices}
        self.assertEqual(offered, {Trough.STATUS_LOADING})

    def test_forged_post_illegal_transition_rejected_by_model(self):
        self.client.force_login(self.user)
        t = make_trough("F-2")  # 装叶中，页面只给萎凋中
        response = self.client.post(
            reverse("trough_edit", args=[t.pk]),
            {
                "garden": str(self.garden.pk),
                "troughCode": "F-2",
                "cultivar": "品种",
                "loadKg": "10.00",
                "status": Trough.STATUS_READY,  # 伪造：装叶中→可下槽
            },
        )
        self.assertEqual(response.status_code, 200)  # 回到表单显示错误
        self.assertContains(response, "非法状态迁移")
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_LOADING)

    def test_forged_post_invalid_status_code_rejected(self):
        self.client.force_login(self.user)
        t = make_trough("F-3")
        response = self.client.post(
            reverse("trough_edit", args=[t.pk]),
            {
                "garden": str(self.garden.pk),
                "troughCode": "F-3",
                "cultivar": "品种",
                "loadKg": "10.00",
                "status": "discarded",  # 伪造一个不存在的状态码
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_LOADING)


class CountsAndFilterConsistencyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="pw")
        self.client.force_login(self.user)

    def _home_numbers(self):
        content = self.client.get(reverse("home")).content.decode()
        import re

        # 三张状态卡：按标签前的数字抓取
        nums = {}
        for code, label in [
            (Trough.STATUS_LOADING, "装叶中"),
            (Trough.STATUS_WITHERING, "萎凋中"),
            (Trough.STATUS_READY, "可下槽"),
        ]:
            m = re.search(r'num">(\d+)</div><div class="label">' + label, content)
            nums[code] = int(m.group(1))
        return nums

    def _filtered_rows(self, status):
        content = self.client.get(
            reverse("trough_list"), {"status": status}
        ).content.decode()
        return content.count('class="badge badge-')

    def test_cards_match_filter_rows_and_counts_after_transitions(self):
        # 初始：三个槽全部装叶中
        t1 = make_trough("K-1")
        t2 = make_trough("K-2")
        t3 = make_trough("K-3")

        def assert_consistent():
            counts = Trough.status_counts()
            home = self._home_numbers()
            for code in ALL_STATUSES:
                rows = self._filtered_rows(code)
                self.assertEqual(rows, counts[code], f"{code} 列表行数 != 计数")
                self.assertEqual(home[code], counts[code], f"{code} 首页卡 != 计数")
                self.assertEqual(rows, home[code], f"{code} 卡与列表不一致")

        assert_consistent()  # 3/0/0

        # 一个槽 装叶中→萎凋中：2/1/0
        transition(t1, Trough.STATUS_WITHERING)
        assert_consistent()

        # 含水率达标后进可下槽：2/0/1
        add_batch(t1, "39.00")
        transition(t1, Trough.STATUS_READY)
        assert_consistent()

        # 再走两个槽，达到 1/1/1
        transition(t2, Trough.STATUS_WITHERING)
        assert_consistent()
        transition(t3, Trough.STATUS_WITHERING)
        add_batch(t3, "36.50")
        transition(t3, Trough.STATUS_READY)
        transition(t1, Trough.STATUS_LOADING)  # 可下槽回到装叶中
        assert_consistent()  # 1(t1)/1(t2)/1(t3)

        # 全部列表行数等于三卡之和
        all_content = self.client.get(reverse("trough_list")).content.decode()
        self.assertEqual(all_content.count('class="badge badge-'), 3)
        self.assertEqual(sum(Trough.status_counts().values()), 3)

    def test_invalid_status_param_is_ignored(self):
        make_trough("K-9")
        resp = self.client.get(reverse("trough_list"), {"status": "bogus"})
        self.assertEqual(resp.context["object_list"].count(), 1)


class SeedTests(TestCase):
    def test_seed_covers_all_three_statuses_via_legal_edges(self):
        from .seed import ensure_seed_data

        ensure_seed_data()
        present = set(Trough.objects.values_list("status", flat=True))
        self.assertEqual(present, set(ALL_STATUSES))
        # 可下槽的槽最新批次必须满足含水率门槛
        for t in Trough.objects.filter(status=Trough.STATUS_READY):
            latest = t.latest_batch()
            self.assertIsNotNone(latest.actualMoisture)
            self.assertLessEqual(latest.actualMoisture, Decimal("40"))

    def test_seed_is_idempotent(self):
        from .seed import ensure_seed_data

        ensure_seed_data()
        ensure_seed_data()
        self.assertEqual(Trough.objects.count(), 3)
