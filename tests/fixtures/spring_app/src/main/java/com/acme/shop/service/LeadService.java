package com.acme.shop.service;

import com.acme.shop.repo.LeadJdbcRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

@Service
@RequiredArgsConstructor
public class LeadService {
    private final LeadJdbcRepository leads;
    private final RestTemplate http;

    public Object list(String status) { return leads.findByStatus(status); }
    public void cancel(long id) { leads.cancel(id); }
}
