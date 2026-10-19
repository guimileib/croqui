package com.acme.shop.domain;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;

@Entity
public class OrderItem {
    @Id
    private Long id;

    @ManyToOne
    @JoinColumn(name = "owner_id")
    private User owner;

    private java.math.BigDecimal unitPrice;

    private String productName;
}
