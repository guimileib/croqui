package demo

import jakarta.persistence.*

@Entity
@Table(name = "products")
data class Product(
    @Id @GeneratedValue val id: Long? = null,
    @Column(name = "product_name", nullable = false) val name: String,
    val unitPrice: Double,
    @ManyToOne @JoinColumn(name = "category_id") val category: Category? = null,
)

@Entity
class Category(@Id val id: Long, val title: String)
