from django.db import connection
from django.http import JsonResponse
from rest_framework import viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from . import selectors, services
from .models import Customer, Product
from .serializers import ProductSerializer


@api_view(["GET", "POST"])
def order_list(request):
    """List recent orders or place a new one."""
    if request.method == "POST":
        order = services.place_order(request.data["customer"], request.data["lines"])
        return Response({"id": order.id}, status=201)
    return Response([o.id for o in selectors.recent_orders()])


@api_view(["POST"])
def refund_order(request, order_id):
    return Response({"status": services.RefundService().refund(order_id).status})


class CustomerDetail(APIView):
    """One customer."""

    def get(self, request, pk):
        return Response(Customer.objects.get(pk=pk).name)

    def delete(self, request, pk):
        customer = Customer.objects.get(pk=pk)
        customer.delete()
        return Response(status=204)


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer

    @action(detail=True, methods=["post"])
    def restock(self, request, pk=None):
        Product.objects.filter(pk=pk).update(stock=100)
        return Response(status=204)


def health(request):
    return JsonResponse({"ok": True})


def customer_count(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM shop_customers")
        return JsonResponse({"count": cursor.fetchone()[0]})
