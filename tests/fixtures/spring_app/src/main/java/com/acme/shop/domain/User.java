package com.acme.shop.domain;

import jakarta.persistence.*;

@Entity
@Table(name = "tb_users", schema = "core")
public class User {
    @Id
    @GeneratedValue
    private Long id;

    @Column(name = "email_address", nullable = false)
    private String email;

    private String displayName;

    @Transient
    private String password;

    @OneToMany(mappedBy = "owner")
    private java.util.List<Object> things;
}
