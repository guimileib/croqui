package demo

import org.springframework.web.bind.annotation.*

object Paths {
    const val ROOT = "/products"
}

@RestController
@RequestMapping(Paths.ROOT)
class ProductController(private val service: ProductService) {

    @GetMapping("/{id}")
    fun get(@PathVariable id: Long) = service.get(id)

    @GetMapping("/search/{name}")
    fun search(@PathVariable name: String, @RequestParam(defaultValue = "1") page: Int): List<Product> {
        val label = "page ${page} of ${"x"}"   // template with nested quotes must not break the matcher
        return service.byName(name)
    }

    @PostMapping
    fun create(@RequestBody p: Product) = service.add(p)

    // @DeleteMapping("/gone")
    @GetMapping("/status")
    fun status(): String = "up"
}
