package com.acme.shop.api;

import com.acme.shop.service.LeadService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/leads")
@RequiredArgsConstructor
public class LeadController {
    private final LeadService leadService;

    @GetMapping
    public Object list(@RequestParam(required = false) String status) {
        return leadService.list(status);
    }

    @PostMapping("/{id}/cancel")
    public void cancel(@PathVariable long id) {
        leadService.cancel(id);
    }

    @GetMapping("/health")
    public String health() { return "ok"; }
}
