package com.acme.shop.service;

import com.acme.shop.repo.OrderJdbcRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

@Service
@RequiredArgsConstructor
public class OrderService {
    private final OrderJdbcRepository orders;
    private final RestTemplate http;

    public Object list(String status) { return orders.findByStatus(status); }
    public void cancel(long id) { orders.cancel(id); }
}
