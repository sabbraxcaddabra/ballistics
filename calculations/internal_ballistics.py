import numpy as np
from numba import jit_module

from ..error_classes import *

# TODO Попробовать все это скомпилировать ahead-of-time

def runge_kutta4_stepper(f, y0, t0, t_end, tau, args=tuple()):
    n = 0
    K = np.zeros((4, len(y0)))
    ys = y0.copy()
    while t0 < t_end:
        f(K[0], ys, *args)
        f(K[1], ys + tau * K[0] / 2, *args)
        f(K[2], ys + tau * K[1] / 2, *args)
        f(K[3], ys + tau * K[2], *args)
        ys += + tau*(K[0] + 2*K[1] + 2*K[2] + K[3])/6
        t0 += tau
        n += 1
        yield n, t0, ys

def P(y, igniter, lambda_khi, S, W0, qfi, omega_sum, psis, powders):
    thet = theta(psis, igniter, powders)
    fs = igniter.fs
    for i, powder in enumerate(powders):
        fs += powder.f_powd * powder.omega * psis[i]
        W0 -= powder.omega * ((1. - psis[i]) / powder.rho + psis[i] * powder.alpha)
    fs -= thet * y[0] ** 2 * (qfi / 2 + lambda_khi * omega_sum / 6.)
    W0 += y[1] * S
    if W0 < 0.:
        raise TooMuchPowderError()
    p_mean = 1e5 + fs / W0
    p_sn = (p_mean/(1 + (1/3) * (lambda_khi*omega_sum)/qfi))*(y[0] > 0.) + (y[0] == 0.)*p_mean
    p_kn = (p_sn*(1 + 0.5*lambda_khi*omega_sum/qfi))*(y[0] > 0.) + (y[0] == 0.)*p_mean

    return p_mean, p_sn, p_kn

def theta(psis, igniter, powders):
    num = igniter.num
    denum = igniter.denum
    for i, powder in enumerate(powders):
        num += powder.f_powd * powder.omega * psis[i] / powder.Ti
        denum += powder.f_powd * powder.omega * psis[i] / (powder.Ti * powder.teta)
    if denum != 0:
        return num / denum
    else:
        return 0.4

def psi(z, zk, kappa1, lambd1, mu1, kappa2, lambd2, mu2):
    if z < 1:
        return kappa1*z*(1 + lambd1*z + mu1*z**2)
    elif 1 <= z <= zk:
        z1 = z - 1
        psiS = kappa1 + kappa1*lambd1 + kappa1*mu1
        return psiS + kappa2*z1*(1 + lambd2*z1 + mu2*z1**2)
    else:
        return 1

def int_bal_rs(dy, y, psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders):
    """
    Функция правых частей системы уравнений внутреней баллистики при аргументе t
    """
    for i, powder in enumerate(powders):
        psis[i] = psi(y[2 + i], *powder[7:])

    lambda_khi = (y[1] + l_k)/(y[1] + l_ps)

    p_mean, p_sn, p_kn = P(y, igniter, lambda_khi, S, W0, qfi, omega_sum, psis, powders)

    if y[0] == 0. and p_mean < P0:
        dy[0] = 0.
        dy[1] = 0.
    else:
        dy[0] = p_sn*S/qfi
        dy[1] = y[0]
    for i, powder in enumerate(powders):
        if p_mean <= 50e6:
            dy[2+i] = ((k50*p_mean**0.75)/powder.Jk) * (y[2+i] < powder.Zk)
        else:
            dy[2+i] = (p_mean/powder.Jk) * (y[2+i] < powder.Zk)
    return p_mean, p_sn, p_kn

def dense_count_ib(P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, l_d, powders, tmax = 1. , tstep = 1e-5):
    n_powd = len(powders)
    y = np.zeros(2+n_powd)
    psis = np.zeros(n_powd)

    args = (psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders)

    ts = np.arange(0, tmax+tstep, tstep)
    ys = np.zeros((ts.shape[0], y.shape[0]))
    psis_array = np.zeros((ys.shape[0], n_powd))
    p_mean, p_sn, p_kn = np.zeros(ys.shape[0]), np.zeros(ys.shape[0]), np.zeros(ys.shape[0])

    for n, ts_n, ys_n in runge_kutta4_stepper(int_bal_rs, y, 0., tmax, tstep, args):
        if ys_n[1] > l_d:
            ts = ts[:n]
            ys = ys[:n].T
            psis_array = psis_array[:n]
            p_mean = p_mean[:n]
            p_sn = p_sn[:n]
            p_kn = p_kn[:n]
            break
        else:
            ys[n] = ys_n
            for i, powd in enumerate(powders):
                psis_array[n, i] = psi(ys_n[2 + i], *powd[7:])
            lambda_khi = (ys_n[1] + l_k) / (ys_n[1] + l_ps)
            p_mean[n], p_sn[n], p_kn[n] = P(ys_n, igniter, lambda_khi, S, W0, qfi, omega_sum, psis_array[n], powders)

    psis_array = psis_array.T
    burned = np.abs(psis_array - 1.)
    lk_indexes = burned.argmin(axis=1).astype('int32')

    # lk_indexes = np.array([burned_a.argmin() for burned_a in burned])

    ys[2:] = psis_array

    return ts, ys, p_mean, p_sn, p_kn, lk_indexes

def fast_count_ib(P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, l_d, powders, tmax = 1., tstep = 1e-5):
    """

    :param P0: Давление форсирование
    :param igniter: Воспламенитель(named tuple)
    :param k50: Константа в законе горения
    :param S: Приведенная площадь канала ствола
    :param W0: Объем каморы
    :param l_k: Длина зарядной каморы
    :param l_ps: Приведенная длина свободного объема каморы
    :param omega_sum: Суммарная масса пороха
    :param qfi: Фиктивная масса снаряда
    :param l_d: Полный путь снаряда
    :param powders: Массив порохов(кортеж из named tuple)
    :param tmax: Максимальное время выстрела
    :param tstep: Шаг по времени
    :return:
    """
    y = np.zeros(2+len(powders))
    lk = 0. # Координата по стволу, соответсвующая полному сгоранию порохового заряда
    t0 = 0. # Начальное время
    p_mean_max = 1e5
    p_sn_max = 1e5
    p_kn_max = 1e5

    K = np.zeros((4, len(y)))

    psis = np.zeros(len(powders))

    while y[1] <= l_d:

        # Проверка условия сгорания всего заряда
        if np.all(np.abs(psis - 1.) < 1e-4) and lk == 0.:
            lk = y[1]

        p_mean1, p_sn1, p_kn1 = int_bal_rs(K[0], y, psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders)
        p_mean2, p_sn2, p_kn2 = int_bal_rs(K[1], y + tstep * K[0] / 2, psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders)
        p_mean3, p_sn3, p_kn3 = int_bal_rs(K[2], y + tstep * K[1] / 2, psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders)
        p_mean4, p_sn4, p_kn4 = int_bal_rs(K[3], y + tstep * K[2], psis, P0, igniter, k50, S, W0, l_k, l_ps, omega_sum, qfi, powders)
        y += tstep*(K[0] + 2*K[1] + 2*K[2] + K[3])/6
        t0 += tstep
        p_mean_max = max(p_mean1, p_mean2, p_mean3, p_mean4, p_mean_max)
        p_sn_max = max(p_sn1, p_sn2, p_sn3, p_sn4, p_sn_max)
        p_kn_max = max(p_kn1, p_kn2, p_kn3, p_kn4, p_kn_max)

        if t0 > tmax:
            raise TooMuchTimeError()

    psi_sum = 0
    for i, powder in enumerate(powders):
        y[2+i] = psi(y[2 + i], *powder[7:])
        psi_sum += y[2+i]*powder.omega
    psi_sum /= omega_sum
    return y[0], p_mean_max, p_sn_max, p_kn_max, psi_sum, lk/l_d

jit_module()
