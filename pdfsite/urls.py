"""
URL configuration for pdfsite project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static
from .views import disabled_pdf_flow_view, home_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", home_view, name="home"),
    path("process/", disabled_pdf_flow_view, name="process"),
    path("result/<int:pk>/", disabled_pdf_flow_view, name="result"),
    path("download/<int:pk>/", disabled_pdf_flow_view, name="download"),
    path("ocr/", include("ocr.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
