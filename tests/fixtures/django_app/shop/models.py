from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Customer(TimeStamped):
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)

    class Meta:
        db_table = "shop_customers"


class Product(models.Model):
    sku = models.CharField(max_length=40, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.IntegerField(default=0)


class Lead(TimeStamped):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="leads")
    status = models.CharField(max_length=20, default="new")


class LeadLine(models.Model):
    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
