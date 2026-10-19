from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"products", views.ProductViewSet)

urlpatterns = [
    path("orders/", views.order_list, name="order-list"),
    path("orders/<int:order_id>/refund/", views.refund_order),
    path("customers/<int:pk>/", views.CustomerDetail.as_view()),
    re_path(r"^stats/customers/$", views.customer_count),
    path("health/", views.health),
    path("", include(router.urls)),
]
