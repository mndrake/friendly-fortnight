             PGM        PARM(&PERIOD)
             DCL        VAR(&PERIOD) TYPE(*CHAR) LEN(2)
             DCL        VAR(&FNAME) TYPE(*CHAR) LEN(10)
             /* Dynamic file name: cannot be resolved statically */
             CHGVAR     VAR(&FNAME) VALUE('ORD' *CAT &PERIOD)
             OVRDBF     FILE(ORDERS) TOFILE(&FNAME)
             CALL       PGM(RPT002)
             ENDPGM
