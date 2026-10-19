package demo

import org.springframework.stereotype.Service

@Service
class ProductService(private val repository: ProductRepository) {
    fun get(id: Long) = repository.findById(id)
    fun byName(name: String) = repository.findByName(name)
    fun add(p: Product) = repository.save(p)
}
