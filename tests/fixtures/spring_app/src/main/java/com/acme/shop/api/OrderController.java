package com.acme.shop.api;

import com.acme.shop.service.OrderService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
@RequiredArgsConstructor
public class OrderController {
    private final OrderService orderService;

    @GetMapping
    public Object list(@RequestParam(required = false) String status) {
        return orderService.list(status);
    }

    @PostMapping("/{id}/cancel")
    public void cancel(@PathVariable long id) {
        orderService.cancel(id);
    }

    @GetMapping("/health")
    public String health() { return "ok"; }
}
