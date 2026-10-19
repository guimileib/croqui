package demo

import org.springframework.data.jpa.repository.JpaRepository
import org.springframework.data.jpa.repository.Modifying
import org.springframework.data.jpa.repository.Query

interface ProductRepository : JpaRepository<Product, Long> {
    fun findByName(name: String): List<Product>

    @Modifying
    @Query("DELETE FROM Product p WHERE p.unitPrice < :min")
    fun purgeCheap(min: Double): Int
}
