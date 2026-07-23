             PGM
             /* Monthly reporting driver: redirect ORDERS to history, run  */
             /* the report, then extract.                                  */
             OVRDBF     FILE(ORDERS) TOFILE(APPLIB/ORDHIST) +
                          OVRSCOPE(*CALLLVL)
             CALL       PGM(RPT001)
             DLTOVR     FILE(ORDERS)
             CALL       PGM(SQLEXT)
             CPYF       FROMFILE(APPLIB/ORDERS) TOFILE(APPLIB/ORDARC) +
                          MBROPT(*REPLACE) CRTFILE(*YES)
             ENDPGM
