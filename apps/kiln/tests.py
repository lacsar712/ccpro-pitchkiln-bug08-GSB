from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, Client
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .services.floor_rules import open_runs_for_lot


def make_lot(code="脂-TEST-1"):
    return ResinLot.objects.create(
        lotCode=code,
        originPlace="松脂坳",
        arrivalKg=Decimal("100.00"),
        receivedAt=timezone.now(),
    )


def make_hearth(tag="灶-试"):
    return FireHearth.objects.create(lane=1, tag=tag, resinGrade="特级脂")


def make_run(hearth, lot, closed=False):
    return CookRun.objects.create(
        hearth=hearth,
        resinLot=lot,
        openedAt=timezone.now(),
        closedAt=timezone.now() if closed else None,
        targetSoftPointC=Decimal("90.00"),
    )


class DeleteResinLotRulesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def test_open_run_blocks_delete_and_keeps_db_intact(self):
        lot = make_lot()
        hearth = make_hearth()
        run = make_run(hearth, lot)
        probe = SoftPointProbe.objects.create(
            run=run, sampledAt=timezone.now(), softPointC=Decimal("92"), samplerName="甲"
        )

        resp = self.client.post(
            f"/resin-lots/{lot.pk}/delete/", follow=True
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "未收灶值守")
        # 批号、值守、探针全部完好，引用未被掏空
        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())
        run.refresh_from_db()
        self.assertEqual(run.resinLot_id, lot.pk)
        self.assertTrue(CookRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(SoftPointProbe.objects.filter(pk=probe.pk).exists())

    def test_closed_run_does_not_block_delete(self):
        lot = make_lot()
        hearth = make_hearth()
        run = make_run(hearth, lot, closed=True)

        resp = self.client.post(
            f"/resin-lots/{lot.pk}/delete/", follow=True
        )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResinLot.objects.filter(pk=lot.pk).exists())
        # 已收灶值守按 SET_NULL 留痕：行和探针还在，批号置空
        run.refresh_from_db()
        self.assertIsNone(run.resinLot_id)
        self.assertTrue(CookRun.objects.filter(pk=run.pk).exists())

    def test_lot_without_runs_is_deletable(self):
        lot = make_lot()
        resp = self.client.post(
            f"/resin-lots/{lot.pk}/delete/", follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResinLot.objects.filter(pk=lot.pk).exists())

    def test_only_open_run_blocks_when_mixed_open_and_closed(self):
        lot = make_lot()
        make_run(make_hearth("灶-收"), lot, closed=True)
        open_run = make_run(make_hearth("灶-开"), lot, closed=False)

        resp = self.client.post(
            f"/resin-lots/{lot.pk}/delete/", follow=True
        )

        self.assertContains(resp, "灶-开")
        self.assertNotContains(resp, "已删除")
        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())
        open_run.refresh_from_db()
        self.assertEqual(open_run.resinLot_id, lot.pk)

    def test_model_layer_blocks_direct_and_queryset_delete(self):
        lot = make_lot()
        make_run(make_hearth(), lot)

        with transaction.atomic():
            with self.assertRaises(ValidationError):
                lot.delete()
        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())

        with transaction.atomic():
            with self.assertRaises(ValidationError):
                ResinLot.objects.filter(pk=lot.pk).delete()
        self.assertTrue(ResinLot.objects.filter(pk=lot.pk).exists())

    def test_open_runs_for_lot_excludes_closed(self):
        lot = make_lot()
        make_run(make_hearth("灶-收"), lot, closed=True)
        make_run(make_hearth("灶-开"), lot, closed=False)
        self.assertEqual(open_runs_for_lot(lot).count(), 1)


class BoardAfterDeleteAndDirtyDataTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def test_board_and_feed_open_after_legit_delete_and_counts_reconcile(self):
        # 批号 A：仅有已收灶值守 → 允许删除
        lot_a = make_lot("脂-A")
        h_a = make_hearth("灶-A")
        run_a = make_run(h_a, lot_a, closed=True)
        # 批号 B：仍有未收灶值守 → 删除被拦
        lot_b = make_lot("脂-B")
        h_b = make_hearth("灶-B")
        run_b = make_run(h_b, lot_b, closed=False)

        self.client.post(f"/resin-lots/{lot_a.pk}/delete/")
        self.client.post(f"/resin-lots/{lot_b.pk}/delete/")

        # 看板、网格、抽屉、来脂批流全部可打开
        board = self.client.get("/")
        self.assertEqual(board.status_code, 200)
        self.assertEqual(self.client.get("/floor/grid/").status_code, 200)
        self.assertEqual(
            self.client.get(
                f"/hearth/{h_b.pk}/drawer/", HTTP_HX_REQUEST="true"
            ).status_code,
            200,
        )
        feed = self.client.get("/resin-lots/")
        self.assertEqual(feed.status_code, 200)

        # 流中 A 已消失、B 仍在
        self.assertNotContains(feed, "脂-A")
        self.assertContains(feed, "脂-B")

        # 可复算：全局未收灶值守只剩 B 的 1 个，看板瓦片仍显示 B 批号
        self.assertEqual(
            CookRun.objects.filter(closedAt__isnull=True).count(), 1
        )
        self.assertEqual(open_runs_for_lot(lot_b).count(), 1)
        grid = self.client.get("/floor/grid/").content.decode()
        self.assertIn("脂-B", grid)
        self.assertNotIn("脂-A", grid)
        # A 的已收灶值守仍留痕
        self.assertTrue(
            CookRun.objects.filter(pk=run_a.pk, resinLot__isnull=True).exists()
        )
        run_b.refresh_from_db()
        self.assertEqual(run_b.resinLot_id, lot_b.pk)

    def test_board_survives_legacy_null_lot_on_open_run(self):
        hearth = make_hearth("灶-脏1")
        run = make_run(hearth, None)  # 旧脏数据：未收灶但批号为空

        for url, kw in [
            ("/", {}),
            (f"/?hearth={hearth.pk}", {}),
            ("/floor/grid/", {}),
            (f"/hearth/{hearth.pk}/drawer/", {"HTTP_HX_REQUEST": "true"}),
            ("/resin-lots/", {}),
        ]:
            resp = self.client.get(url, **kw)
            self.assertEqual(resp.status_code, 200, url)

        grid = self.client.get("/floor/grid/").content.decode()
        self.assertIn("未绑定批号", grid)


class LegacyOrphanFkBoardTests(TransactionTestCase):
    """悬空外键脏数据（批号行已不存在但值守仍指向它）的看板兜底。

    必须用 TransactionTestCase：PRAGMA foreign_keys 在事务内是 no-op，
    无法在 TestCase 的包裹事务里构造悬空外键。
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")
        self.client = Client()
        self.client.force_login(self.user)

    def test_board_survives_orphaned_lot_fk_on_open_run(self):
        hearth = make_hearth("灶-脏2")
        make_run(hearth, None)
        with connection.cursor() as cur:
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute(
                "UPDATE kiln_cookrun SET resinLot_id = 999999 "
                "WHERE hearth_id = %s",
                [hearth.pk],
            )
            cur.execute("PRAGMA foreign_keys=ON")

        for url, kw in [
            ("/", {}),
            (f"/?hearth={hearth.pk}", {}),
            ("/floor/grid/", {}),
            (f"/hearth/{hearth.pk}/drawer/", {"HTTP_HX_REQUEST": "true"}),
        ]:
            resp = self.client.get(url, **kw)
            self.assertEqual(resp.status_code, 200, url)

        grid = self.client.get("/floor/grid/").content.decode()
        self.assertIn("未绑定批号", grid)
