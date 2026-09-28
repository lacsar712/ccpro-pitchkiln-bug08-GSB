from django.contrib import admin

from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .services.floor_rules import open_runs_for_lot


@admin.register(ResinLot)
class ResinLotAdmin(admin.ModelAdmin):
    list_display = ("id", "lotCode", "originPlace", "arrivalKg", "receivedAt")
    search_fields = ("lotCode", "originPlace")

    def get_deleted_objects(self, objs, request):
        (
            deleted_objects,
            model_count,
            perms_needed,
            protected,
        ) = super().get_deleted_objects(objs, request)
        # 删除确认页直接拦住仍挂未收灶值守的来脂批
        for lot in objs:
            for run in open_runs_for_lot(lot).select_related("hearth"):
                protected.append(run)
        return deleted_objects, model_count, perms_needed, protected


@admin.register(FireHearth)
class FireHearthAdmin(admin.ModelAdmin):
    list_display = ("id", "lane", "tag", "resinGrade", "phase")
    list_filter = ("phase", "lane")
    search_fields = ("tag", "resinGrade")


@admin.register(CookRun)
class CookRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "hearth",
        "resinLot",
        "openedAt",
        "closedAt",
        "targetSoftPointC",
    )
    list_filter = ("hearth",)
    search_fields = ("hearth__tag", "resinLot__lotCode")


@admin.register(SoftPointProbe)
class SoftPointProbeAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "sampledAt", "softPointC", "samplerName")
    search_fields = ("samplerName",)
