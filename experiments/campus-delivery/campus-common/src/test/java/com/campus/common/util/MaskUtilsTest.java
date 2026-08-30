package com.campus.common.util;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class MaskUtilsTest {

    @Test
    void phoneMasksMiddle4Digits() {
        assertEquals("138****5678", MaskUtils.phone("13812345678"));
        assertEquals("139****8000", MaskUtils.phone("13900138000"));
    }

    @Test
    void shortPhoneUnchanged() {
        assertEquals("13812", MaskUtils.phone("13812"));
        assertEquals(null, MaskUtils.phone(null));
    }

    @Test
    void nameKeepsFirstChar() {
        assertEquals("张*", MaskUtils.name("张三"));
        assertEquals("欧***", MaskUtils.name("欧阳娜娜"));
        assertEquals("李", MaskUtils.name("李"));
        assertEquals(null, MaskUtils.name(null));
        assertEquals("", MaskUtils.name(""));
    }
}
