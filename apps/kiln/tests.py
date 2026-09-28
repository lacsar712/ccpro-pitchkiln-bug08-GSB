from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot

User = get_user_model()


class DeleteResinLotTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "pw123456"
        )
        self.client.force_login(self.user)
        self.now = timezone.now()
        self.hearth = FireHearth.objects.create(
            lane=1, tag="灶-测试", resinGrade="特级", phase=FireHearth.PHASE_COLD
        )

    def _lot(self, code="脂-测试-1"):
        return ResinLot.objects.create(
            lotCode=code,
            originPlace="测试地",
            arrivalKg=Decimal("100.00"),
            receivedAt=self.now,
        )

    def _run(self, lot, closed):
        return CookRun.objects.create(
            hearth=self.hearth,
            resinLot=lot,
            openedAt=self.now - timezone.timedelta(hours=2),
            closedAt=self.now if closed else None,
            targetSoftPointC=Decimal("90.00"),
        )

    def test_open_run_blocks_delete_and_keeps_db_intact(self):
        lot = self._lot()
        run = self._run(lot, closed=False)

        resp = self.client.post(
            reverse("delete_resin_lot", args=[lot.pk]), follow=True
        )

        self.assertEqual(resp.status_code, 200)
        # 批号仍在
        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())
        # 值守仍在且引用未被掏空
        run.refresh_from_db()
        self.assertEqual(run.resinLot_id, lot.pk)
        self.assertContains(resp, "未收灶值守")

    def test_mixed_open_and_closed_still_blocked(self):
        lot = self._lot("脂-测试-2")
        self._run(lot, closed=True)
        open_run = self._run(lot, closed=False)

        self.client.post(reverse("delete_resin_lot", args=[lot.pk]))

        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())
        open_run.refresh_from_db()
        self.assertEqual(open_run.resinLot_id, lot.pk)

    def test_only_closed_runs_allows_delete(self):
        lot = self._lot("脂-测试-3")
        closed_run = self._run(lot, closed=True)

        resp = self.client.post(
            reverse("delete_resin_lot", args=[lot.pk]), follow=True
        )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResinLot.objects.filter(pk=lot.pk).exists())
        # 历史值守保留，引用按 SET_NULL 脱钩
        closed_run.refresh_from_db()
        self.assertIsNone(closed_run.resinLot_id)
        self.assertContains(resp, "来脂批已删除")

    def test_board_and_feed_open_after_legit_delete(self):
        lot = self._lot("脂-测试-4")
        self._run(lot, closed=True)
        self.client.post(reverse("delete_resin_lot", args=[lot.pk]))

        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("floor_grid")).status_code, 200)
        self.assertEqual(self.client.get(reverse("resin_lot_feed")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("hearth_drawer", args=[self.hearth.pk]),
                HTTP_HX_REQUEST="true",
            ).status_code,
            200,
        )


class OrphanOpenRunDirtyDataTests(TestCase):
    """旧脏数据：未收灶值守的来脂批被掏空（resinLot=NULL），看板必须兜住。"""

    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "pw123456"
        )
        self.client.force_login(self.user)
        now = timezone.now()
        self.hearth = FireHearth.objects.create(
            lane=1, tag="灶-孤儿", resinGrade="特级", phase=FireHearth.PHASE_HOLDING
        )
        self.orphan = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=None,
            openedAt=now - timezone.timedelta(hours=1),
            closedAt=None,
            targetSoftPointC=Decimal("90.00"),
        )

    def test_board_survives_orphan_open_run(self):
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "未绑定来脂批")

    def test_drawer_survives_orphan_open_run(self):
        resp = self.client.get(
            reverse("hearth_drawer", args=[self.hearth.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "未绑定来脂批")

    def test_grid_and_feed_survive_orphan_open_run(self):
        self.assertEqual(self.client.get(reverse("floor_grid")).status_code, 200)
        self.assertEqual(self.client.get(reverse("resin_lot_feed")).status_code, 200)


class BoardCountsRecomputeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "pw123456"
        )
        self.client.force_login(self.user)
        self.now = timezone.now()

    def test_feed_card_count_matches_db(self):
        for i in range(3):
            ResinLot.objects.create(
                lotCode=f"脂-卡-{i}",
                originPlace="地",
                arrivalKg=Decimal("1"),
                receivedAt=self.now - timezone.timedelta(minutes=i),
            )
        resp = self.client.get(reverse("resin_lot_feed"))
        self.assertEqual(ResinLot.objects.count(), 3)
        for i in range(3):
            self.assertContains(resp, f"脂-卡-{i}")
        self.assertNotContains(resp, "暂无来脂批")

    def test_open_run_tile_reflects_lot_after_blocked_delete(self):
        lot = ResinLot.objects.create(
            lotCode="脂-看板-1",
            originPlace="地",
            arrivalKg=Decimal("1"),
            receivedAt=self.now,
        )
        hearth = FireHearth.objects.create(
            lane=9, tag="灶-看板", resinGrade="一级", phase=FireHearth.PHASE_RAMPING
        )
        CookRun.objects.create(
            hearth=hearth,
            resinLot=lot,
            openedAt=self.now,
            closedAt=None,
            targetSoftPointC=Decimal("88"),
        )

        # 删除被挡
        self.client.post(reverse("delete_resin_lot", args=[lot.pk]))
        # 看板瓦片仍能复算出该未收灶值守及其批号
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, lot.lotCode)
