package com.acme.shop.service;

public interface UserService {
    Object find(Long id);
    Object create(Object body);
    Object update(Long id, Object body);
    void remove(Long id);
}
