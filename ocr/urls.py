from django.urls import path

from . import views

app_name = "ocr"

urlpatterns = [
    path("upload/", views.upload_sheet_view, name="upload"),
    path("review/<int:pk>/", views.review_sheet_view, name="review"),
    path("debug/<int:pk>/", views.debug_sheet_view, name="debug"),
    path("review/<int:pk>/cell/<int:cell_pk>/correct/", views.correct_cell_view, name="correct_cell"),
    path("confirm/<int:pk>/", views.confirm_sheet_view, name="confirm"),
    path("rerun/<int:pk>/", views.rerun_ocr_view, name="rerun"),
]
