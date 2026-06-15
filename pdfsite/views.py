from django.http import HttpResponse
from django.shortcuts import render


DISABLED_PDF_FLOW_MESSAGE = "Old PDF flow is disabled. Use /ocr/upload/ for handwritten OCR."


def home_view(request):
    return render(request, "home.html")


def disabled_pdf_flow_view(request, *args, **kwargs):
    return HttpResponse(DISABLED_PDF_FLOW_MESSAGE)
